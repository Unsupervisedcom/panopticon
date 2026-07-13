"""The container entrypoint.

The entrypoint owns the deterministic in-container protocol around the agent (the agent is the
only thing that calls an LLM):

1. if the task has no **slug**, set one (the slug hook — slugs are decided in the container,
   unlike cloude-cade, per ARCHITECTURE.md §8.3);
2. connect to the task service and hold a **liveness connection** open — the open connection *is*
   the registration, so death (clean exit or crash) drops it and the service notices immediately;
3. do the work — the agent — alongside it.

Two shapes share that protocol:

* :func:`run_task_container` — one-shot, in-process; the agent is an injected callback. Used by
  the stub runner for the walking skeleton (no Docker). It registers/deregisters explicitly
  (there's no socket to drop in-process).
* :func:`serve` / :func:`main` — the long-lived form a real container runs as
  ``python -m panopticon.container``: set slug, then **hold the liveness connection until
  signalled**, reconnecting if it drops underneath. A clean stop closes the connection (a clean
  deregister); an unclean death drops it (the service reaps on disconnect). This is liveness only;
  the **agent** runs alongside it in the tmux pane via :mod:`panopticon.container.agent` (the
  launcher), so the roles stay separate and ``tmux attach`` reaches the live agent. (No LLM runs
  here or in tests.)
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

import httpx

from panopticon.client import TaskServiceClient

Work = Callable[[TaskServiceClient, str], None]

#: How long to wait before re-opening the liveness connection after it drops underneath a still-
#: running container (a transient network blip). Small: the gap is a brief ``down`` flicker that
#: self-heals on reconnect, and respawn is operator-gated, so nothing auto-acts on the flicker.
RECONNECT_BACKOFF_SECONDS = 1.0


def _set_slug_if_unset(client: TaskServiceClient, task_id: str, proposed_slug: str | None) -> None:
    """The slug hook: set the slug iff the task has none and one was proposed."""
    if proposed_slug is not None and client.get_task(task_id)["slug"] is None:
        client.set_slug(task_id, proposed_slug)


def run_task_container(
    client: TaskServiceClient,
    task_id: str,
    *,
    container_id: str,
    runner_id: str | None = None,
    proposed_slug: str | None = None,
    work: Work | None = None,
) -> None:
    """Run the protocol once, in-process: register → slug → ``work`` → deregister.

    The in-process form has no socket to drop, so it registers and deregisters explicitly rather
    than holding a liveness connection (that's :func:`serve`'s job for a real container)."""
    registration = client.register(task_id, container_id=container_id, runner_id=runner_id)
    try:
        _set_slug_if_unset(client, task_id, proposed_slug)
        if work is not None:
            work(client, task_id)
    finally:
        client.deregister(registration["id"])


def serve(
    client: TaskServiceClient,
    task_id: str,
    *,
    container_id: str,
    runner_id: str | None = None,
    proposed_slug: str | None = None,
    running: Callable[[], bool],
    reconnect_backoff: float = RECONNECT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-lived form: set slug, then hold the liveness connection while ``running()``.

    ``running`` lets a caller decide when to stop (a signal flag in production; a counter in
    tests). The open ``/live`` connection is the liveness signal: the service registers on connect
    and reaps on disconnect. On a clean stop ``running()`` flips and we close the connection (a
    clean deregister); on ``SIGKILL`` (e.g. ``docker rm -f``) the process dies and the dropped
    connection reaps the registration. If the connection drops while still running (a transient
    blip) we reconnect after a short backoff — a brief ``down`` flicker that self-heals.
    """
    _set_slug_if_unset(client, task_id, proposed_slug)
    while running():
        live = client.live(task_id, container_id=container_id, runner_id=runner_id)
        try:
            for _ in live:  # each tick is a server keepalive; recheck whether to stop
                if not running():
                    break
        except httpx.HTTPError:
            pass  # connection dropped underneath us — fall through to reconnect
        finally:
            live.close()  # close the stream → drop the connection → server deregisters
        if running():
            sleep(reconnect_backoff)


def _until_signalled() -> Callable[[], bool]:
    """A ``running`` predicate that flips to False on SIGTERM/SIGINT (e.g. ``docker stop``)."""
    stopped = False

    def _stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return lambda: not stopped


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = TaskServiceClient.from_url,
    running: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Container entrypoint: read ``PANOPTICON_*`` env and serve until signalled."""
    env = os.environ
    client = client_factory(env["PANOPTICON_SERVICE_URL"])
    serve(
        client,
        env["PANOPTICON_TASK_ID"],
        container_id=env["PANOPTICON_CONTAINER_ID"],
        runner_id=env.get("PANOPTICON_RUNNER_ID"),
        proposed_slug=env.get("PANOPTICON_PROPOSED_SLUG"),
        running=running if running is not None else _until_signalled(),
        reconnect_backoff=float(env.get("PANOPTICON_RECONNECT_BACKOFF", RECONNECT_BACKOFF_SECONDS)),
        sleep=sleep,
    )
