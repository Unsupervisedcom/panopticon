"""The per-host session service daemon (ADR 0008/0011) — ``python -m panopticon.sessionservice.host``.

One long-lived loop that, each pass over the task service's tasks, **spawns** new ones (claim →
`prepare_workspace` → container) and **provisions** slugged ones (branch the per-task clone), so a
task created in the dashboard actually comes up and gets its branch with no manual step. The two
sub-steps gate themselves (spawn skips claimed/terminal; provision skips unslugged/already-branched),
so calling both on every task each pass is safe. A transient error on one task is logged and skipped.

It doesn't re-poll on a fixed interval: it *blocks* on the task service's change feed
(``list_tasks_versioned(wait=…, since=…)``), waking only when a task actually changes (or the wait
elapses), so an idle host does no work.

Alongside that loop it **holds a host-liveness connection** (``/runners/{id}/live``,
:func:`hold_runner_liveness`) open for its whole life — the connection-drop liveness PR #146 gave
containers, one layer up — so the control plane knows the host is alive and can **reclaim** its
claims (release them for a healthy host to respawn) when it isn't.

**Two URLs.** The daemon talks to the task service at ``--service-url`` (the host's own view, e.g.
``localhost:8000``), but spawns containers pointed at ``--container-service-url`` (the in-container
view, e.g. ``host.docker.internal:8000``). LLM-free.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from collections.abc import Callable

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.images import ImageBuilder
from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, LocalRunner
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.sessionservice.spawner import Spawner

_log = logging.getLogger(__name__)

DEFAULT_CACHE_ROOT = os.path.expanduser("~/.panopticon/cache")
DEFAULT_TASKS_ROOT = os.path.expanduser("~/.panopticon/tasks")


class HostDaemon:
    """Polls this host's tasks and, each pass, spawns new ones and provisions slugged ones."""

    def __init__(
        self,
        client: TaskServiceClient,
        spawner: Spawner,
        provisioner: Provisioner,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
    ) -> None:
        self._client = client
        self._spawner = spawner
        self._provisioner = provisioner
        self._sleep = sleep
        self._interval = interval

    def tick(self, tasks: list[JsonObj]) -> None:
        """One pass over a task snapshot: spawn each spawnable task, provision each slugged one, and
        reconcile each claimed one's container-lifecycle status (down-detection). All three self-gate,
        so re-running over an unchanged snapshot is a no-op."""
        for task in tasks:
            try:
                self._spawner.spawn_one(task)
                self._provisioner.provision(task)
                self._spawner.reconcile(task)
            except Exception:  # a transient git/REST/FS error on one task must not stall the others
                _log.warning("host pass failed for task %s", task.get("id"), exc_info=True)
                continue

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Wake on task changes (not a fixed interval) until ``until()`` is true (``None`` = forever).

        The loop *blocks* on the task service's change feed — ``list_tasks_versioned(wait=…,
        since=…)`` parks until a task changes past the last version we saw or ``wait`` elapses, then
        returns the current snapshot + its version, which we feed back as ``since`` to wait for the
        *next* change. A quiet period just returns the unchanged snapshot and we re-block, so there's
        no busy loop — the blocking request, not a ``sleep``, paces us.

        A whole-pass failure — the request raising on a service blip or before the service is
        listening (the ``make start`` startup race) — is logged and retried after a short
        ``sleep`` (the only place we sleep: the blocking call returns immediately on a connection
        error, so without it a startup race would spin). A transient error never kills the long-lived
        daemon (and its tmux session). Per-task errors are already isolated inside :meth:`tick`.
        """
        since = 0
        while not (until and until()):
            try:
                tasks, since = self._client.list_tasks_versioned(wait=self._interval, since=since)
                self.tick(tasks)
            except Exception:
                _log.warning("host pass failed; retrying", exc_info=True)
                self._sleep(self._interval)


#: Backoff before re-opening the host-liveness connection after it drops underneath a still-running
#: daemon (a transient blip). Small: the gap is a brief window where the runner reads as down, which
#: only an operator-gated reclaim acts on, so nothing auto-acts on the flicker.
RUNNER_RECONNECT_BACKOFF_SECONDS = 1.0


def hold_runner_liveness(
    client: TaskServiceClient,
    runner_id: str,
    *,
    running: Callable[[], bool],
    reconnect_backoff: float = RUNNER_RECONNECT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Hold this host's liveness connection open while ``running()`` — the container's ``serve``
    loop, one layer up.

    The open ``/runners/{id}/live`` stream *is* the host-liveness signal: the task service marks the
    runner live on connect and drops it from ``live_runners`` the instant the stream closes. On a
    clean stop ``running()`` flips and we close it (a clean deregister); on a crash the dropped
    socket reaps it. If it drops while still running (a transient blip) we reconnect after a short
    backoff, re-asserting the same ``runner_id``, so a dead host stays distinguishable from a flake.
    No heartbeat, no TTL — the connection-drop liveness PR #146 gave containers, now for the host.
    """
    while running():
        live = client.live_runner(runner_id)
        try:
            for _ in live:  # each tick is a server keepalive; recheck whether to stop
                if not running():
                    break
        except httpx.HTTPError:
            pass  # connection dropped underneath us — fall through to reconnect
        finally:
            live.close()  # close the stream → drop the connection → service drops the runner
        if running():
            sleep(reconnect_backoff)


def run_host(
    client: TaskServiceClient,
    runner: LocalRunner,
    *,
    runner_id: str,
    tasks_root: str,
    cache: CloneCache,
    git: GitClones,
    images: ImageBuilder | None = None,
    interval: float = 2.0,
    until: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wire the spawner + provisioner over a shared per-task-clone root and run the host loop."""
    spawner = Spawner(
        client, runner, runner_id=runner_id, cache=cache, tasks_root=tasks_root, git=git, images=images
    )
    provisioner = Provisioner(client, clones_root=tasks_root, git=git)
    HostDaemon(client, spawner, provisioner, interval=interval, sleep=sleep).run(until=until)


def main(argv: list[str] | None = None, *, client: TaskServiceClient | None = None) -> None:  # pragma: no cover - thin wiring + endless loop
    parser = argparse.ArgumentParser(
        prog="python -m panopticon.sessionservice.host",
        description="Per-host session service: spawn tasks + provision them (ADR 0008/0011).",
    )
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", "http://localhost:8000"),
        help="task service URL the daemon polls (the host's own view)",
    )
    parser.add_argument(
        "--container-service-url",
        default=os.environ.get("PANOPTICON_CONTAINER_SERVICE_URL", "http://host.docker.internal:8000"),
        help="task service URL spawned containers call back to (the in-container view)",
    )
    parser.add_argument("--runner-id", default=os.environ.get("PANOPTICON_RUNNER_ID", "local"))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--cache-root", default=os.environ.get("PANOPTICON_CACHE_ROOT", DEFAULT_CACHE_ROOT))
    parser.add_argument("--tasks-root", default=os.environ.get("PANOPTICON_TASKS_ROOT", DEFAULT_TASKS_ROOT))
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="change-feed long-poll wait, seconds (the keepalive ceiling between blocking calls)",
    )
    args = parser.parse_args(argv)
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    runner = LocalRunner(args.container_service_url, image=args.image, runner_id=args.runner_id)
    # Hold this host's liveness connection for the daemon's whole life, alongside the spawn/provision
    # loop, so the control plane knows the host is alive (and can reclaim its claims when it isn't).
    # A daemon thread: it dies with the process, dropping the connection (a clean deregister).
    liveness = threading.Thread(
        target=hold_runner_liveness,
        args=(client, args.runner_id),
        kwargs={"running": lambda: True},
        daemon=True,
    )
    liveness.start()
    run_host(
        client, runner,
        runner_id=args.runner_id, tasks_root=args.tasks_root,
        cache=CloneCache(args.cache_root), git=GitClones(),
        images=ImageBuilder(base=args.image),  # compose workflow layers onto the same base (ADR 0005)
        interval=args.interval,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
