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
  deregistering on exit. This is liveness only; the **agent** runs alongside it in the tmux pane
  via :mod:`panopticon.container.agent` (the launcher), so the roles stay separate and
  ``tmux attach`` reaches the live agent. (No LLM runs here or in tests.)
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

import httpx

from panopticon.client import JsonObj, TaskServiceClient

Work = Callable[[TaskServiceClient, str], None]

HEARTBEAT_INTERVAL = 5.0
#: Boot registration is retried on a transient transport error: the task service can be momentarily
#: unreachable when a container boots — a service restart, a network blip, or a **burst of concurrent
#: container starts** overflowing its accept backlog. Without this the container exits(1) at boot and
#: must be respawned ("takes multiple tries to start"). ~60s budget covers a restart window + bursts.
REGISTER_ATTEMPTS = 60
REGISTER_INTERVAL = 1.0


def _register_resilient(
    client: TaskServiceClient,
    task_id: str,
    *,
    container_id: str,
    runner_id: str | None,
    attempts: int,
    interval: float,
    sleep: Callable[[float], None],
) -> JsonObj:
    """Register, retrying on a transient transport error (connection refused, timeout) until the
    service answers or the attempt budget is spent. A non-transport error (e.g. a 4xx) propagates
    immediately — only the *unreachable service* case is worth retrying."""
    last: httpx.TransportError | None = None
    for _ in range(max(1, attempts)):
        try:
            return client.register(task_id, container_id=container_id, runner_id=runner_id)
        except httpx.TransportError as exc:
            last = exc
            sleep(interval)
    raise last if last is not None else RuntimeError("register: no attempts made")


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
    register_attempts: int = REGISTER_ATTEMPTS,
    register_interval: float = REGISTER_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-lived form: register → slug → heartbeat while ``running()`` → deregister.

    ``running`` lets a caller decide when to stop (a signal flag in production; a counter in
    tests). Registration is retried on a momentarily-unreachable service (see
    :func:`_register_resilient`) so a transient blip at boot doesn't kill the container; a transient
    heartbeat failure mid-life is likewise tolerated (a sustained gap is what the service's liveness
    TTL reaps). On a clean stop the container deregisters; on ``SIGKILL`` (e.g. ``docker rm -f``) the
    process dies without deregistering — which is how lost liveness surfaces.
    """
    registration = _register_resilient(
        client, task_id, container_id=container_id, runner_id=runner_id,
        attempts=register_attempts, interval=register_interval, sleep=sleep,
    )
    try:
        _set_slug_if_unset(client, task_id, proposed_slug)
        while running():
            try:
                client.heartbeat(registration["id"])
            except httpx.TransportError:
                pass  # a transient blip shouldn't kill a live container; the TTL reaps a real death
            sleep(heartbeat_interval)
    finally:
        try:
            client.deregister(registration["id"])
        except httpx.HTTPError:
            pass  # best-effort on shutdown — if the service is unreachable, the TTL reaps it


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
        heartbeat_interval=float(env.get("PANOPTICON_HEARTBEAT_INTERVAL", HEARTBEAT_INTERVAL)),
        register_attempts=int(env.get("PANOPTICON_REGISTER_ATTEMPTS", REGISTER_ATTEMPTS)),
        register_interval=float(env.get("PANOPTICON_REGISTER_INTERVAL", REGISTER_INTERVAL)),
        sleep=sleep,
    )
