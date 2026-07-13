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
from pathlib import Path

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.dirs import CLONE_CACHE_DIR, TASKS_DIR
from panopticon.core.git import GitClones
from panopticon.sessionservice._migration import migrate_session_dirs
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.executions import WorkflowExecutions
from panopticon.sessionservice.images import ImageBuilder
from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, LocalRunner
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.sessionservice.shell_runner import ShellRunner
from panopticon.sessionservice.spawner import Spawner

_log = logging.getLogger(__name__)


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
        """One pass over a task snapshot: spawn each spawnable task, provision each slugged one,
        reconcile each claimed one's container-lifecycle status (down-detection), and heal each
        orphan (a claimed task whose tmux session is gone → respawn). All self-gate, so re-running
        over an unchanged snapshot is a no-op.

        A cheap REST-only **pre-pass flags every orphan ``healing`` first**, before any respawn. The
        respawn loop below is serial (each :meth:`Spawner.heal` blocks on ``docker run`` + the tmux
        session), so marking inside it would surface only the orphan currently coming back and leave
        the queued ones reading ``down``; flagging them all up front lets the dashboard show the
        whole batch healing at once, each clearing to ``live`` as its respawn finishes."""
        for task in tasks:
            try:
                self._spawner.mark_healing(task)
            except Exception:  # best-effort visibility — never let it stall the respawn pass below
                _log.warning("flagging heal failed for task %s", task.get("id"), exc_info=True)
        for task in tasks:
            try:
                self._spawner.spawn_one(task)
                self._provisioner.provision(task)
                self._spawner.reconcile(task)
                self._spawner.heal(task)
                self._spawner.cleanup(task)
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
        startup = True
        while not (until and until()):
            try:
                tasks, since = self._client.list_tasks_versioned(wait=self._interval, since=since)
                if startup:
                    startup = False
                    self._spawner.startup_reclaim(tasks)
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
    host: str | None = None,
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
    ``host`` is passed to the task service so the terminal supervisor can ssh-attach to remote tasks.
    """
    while running():
        live = client.live_runner(runner_id, host=host)
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
    shell_runner: ShellRunner | None = None,
    images: ImageBuilder | None = None,
    makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    interval: float = 2.0,
    until: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wire the spawner + provisioner over a shared per-task-clone root and run the host loop."""
    executions = WorkflowExecutions(client)  # one shared "how is this workflow run" cache for both
    spawner = Spawner(
        client,
        runner,
        runner_id=runner_id,
        cache=cache,
        tasks_root=tasks_root,
        shell_runner=shell_runner,
        executions=executions,
        git=git,
        images=images,
        makedirs=makedirs,
    )
    provisioner = Provisioner(client, clones_root=tasks_root, git=git, executions=executions)
    HostDaemon(client, spawner, provisioner, interval=interval, sleep=sleep).run(until=until)


def main(
    argv: list[str] | None = None, *, client: TaskServiceClient | None = None
) -> None:  # pragma: no cover - thin wiring + endless loop
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
        default=os.environ.get(
            "PANOPTICON_CONTAINER_SERVICE_URL", "http://host.docker.internal:8000"
        ),
        help="task service URL spawned containers call back to (the in-container view)",
    )
    parser.add_argument("--runner-id", default=os.environ.get("PANOPTICON_RUNNER_ID", "local"))
    parser.add_argument(
        "--host",
        default=os.environ.get("PANOPTICON_RUNNER_HOST", ""),
        help="hostname or alias reported to the task service",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="change-feed long-poll wait, seconds (the keepalive ceiling between blocking calls)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    migrate_session_dirs(CLONE_CACHE_DIR, TASKS_DIR)
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    runner = LocalRunner(args.container_service_url, image=args.image, runner_id=args.runner_id)
    # A shell workflow runs directly on the host (no container), so it reaches the task service at
    # the host's own view (--service-url), not the in-container host.docker.internal address.
    shell_runner = ShellRunner(args.service_url, runner_id=args.runner_id)
    # Hold this host's liveness connection for the daemon's whole life, alongside the spawn/provision
    # loop, so the control plane knows the host is alive (and can reclaim its claims when it isn't).
    # A daemon thread: it dies with the process, dropping the connection (a clean deregister).
    liveness = threading.Thread(
        target=hold_runner_liveness,
        args=(client, args.runner_id),
        kwargs={"running": lambda: True, "host": args.host},
        daemon=True,
    )
    liveness.start()
    run_host(
        client,
        runner,
        runner_id=args.runner_id,
        tasks_root=TASKS_DIR,
        cache=CloneCache(CLONE_CACHE_DIR),
        git=GitClones(),
        shell_runner=shell_runner,
        images=ImageBuilder(
            base=args.image
        ),  # compose workflow layers onto the same base (ADR 0005)
        interval=args.interval,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
