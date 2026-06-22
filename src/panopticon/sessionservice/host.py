"""The per-host session service daemon (ADR 0008/0011) — ``python -m panopticon.sessionservice.host``.

One long-lived loop that, each pass over the task service's tasks, **spawns** new ones (claim →
`prepare_workspace` → container) and **provisions** slugged ones (branch the per-task clone), so a
task created in the dashboard actually comes up and gets its branch with no manual step. The two
sub-steps gate themselves (spawn skips claimed/terminal; provision skips unslugged/already-branched),
so calling both on every task each pass is safe. A transient error on one task is logged and skipped.

**Two URLs.** The daemon *polls* the task service at ``--service-url`` (the host's own view, e.g.
``localhost:8000``), but spawns containers pointed at ``--container-service-url`` (the in-container
view, e.g. ``host.docker.internal:8000``). LLM-free.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable

import httpx

from panopticon.client import TaskServiceClient
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

    def tick(self) -> None:
        """One pass: spawn each spawnable task and provision each slugged one (both self-gating)."""
        for task in self._client.list_tasks():
            try:
                self._spawner.spawn_one(task)
                self._provisioner.provision(task)
            except Exception:  # a transient git/REST/FS error on one task must not stall the others
                _log.warning("host pass failed for task %s", task.get("id"), exc_info=True)
                continue

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Poll until ``until()`` is true (``None`` = forever).

        A whole-pass failure — ``list_tasks()`` raising on a service blip or before the service is
        listening (the ``make panopticon`` startup race) — is logged and retried next interval, so a
        transient error never kills the long-lived daemon (and its tmux session) with it. Per-task
        errors are already isolated inside :meth:`tick`; this is the outer net for everything else.
        """
        while not (until and until()):
            try:
                self.tick()
            except Exception:
                _log.warning("host pass failed; retrying next interval", exc_info=True)
            self._sleep(self._interval)


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
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval, seconds")
    args = parser.parse_args(argv)
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    runner = LocalRunner(args.container_service_url, image=args.image, runner_id=args.runner_id)
    run_host(
        client, runner,
        runner_id=args.runner_id, tasks_root=args.tasks_root,
        cache=CloneCache(args.cache_root), git=GitClones(),
        images=ImageBuilder(base=args.image),  # compose workflow layers onto the same base (ADR 0005)
        interval=args.interval,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
