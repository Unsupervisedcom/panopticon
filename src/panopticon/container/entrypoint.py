"""The container entrypoint.

The entrypoint owns the deterministic in-container protocol around the agent (the agent is the
only thing that calls an LLM):

1. connect to the task service and **register** (liveness), staying registered until exit;
2. if the task has no **slug**, set one (the slug hook — slugs are decided in the container,
   unlike cloude-cade, per ARCHITECTURE.md §8.3);
3. do the work — the agent — while heartbeating;
4. deregister on exit.

Two shapes share that protocol:

* :func:`run_task_container` — one-shot, in-process; the agent is an injected callback. Used by
  the stub runner for the walking skeleton (no Docker).
* :func:`serve` / :func:`main` — the long-lived form a real container runs as
  ``python -m panopticon.container``: register, set slug, then **heartbeat until signalled**,
  deregistering on exit. The "agent" here is the heartbeat loop itself — a stay-alive
  placeholder; a real agent invocation replaces it in a later slice. (No LLM runs in tests.)
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

import httpx

from panopticon.container.client import TaskServiceClient

Work = Callable[[TaskServiceClient, str], None]

HEARTBEAT_INTERVAL = 5.0


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
    """Run the protocol once, in-process: register → slug → ``work`` → deregister."""
    registration = client.register(task_id, container_id=container_id, runner_id=runner_id)
    try:
        _set_slug_if_unset(client, task_id, proposed_slug)
        client.heartbeat(registration["id"])
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
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-lived form: register → slug → heartbeat while ``running()`` → deregister.

    ``running`` lets a caller decide when to stop (a signal flag in production; a counter in
    tests). On a clean stop the container deregisters; on ``SIGKILL`` (e.g. ``docker rm -f``)
    the process dies without deregistering — which is how lost liveness surfaces.
    """
    registration = client.register(task_id, container_id=container_id, runner_id=runner_id)
    try:
        _set_slug_if_unset(client, task_id, proposed_slug)
        while running():
            client.heartbeat(registration["id"])
            sleep(heartbeat_interval)
    finally:
        client.deregister(registration["id"])


def _until_signalled() -> Callable[[], bool]:
    """A ``running`` predicate that flips to False on SIGTERM/SIGINT (e.g. ``docker stop``)."""
    stopped = False

    def _stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return lambda: not stopped


def _make_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _make_client,
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
        sleep=sleep,
    )
