"""The session service's observe-and-provision loop (ADR 0010/0011).

Coordination is **pull, not push**: the task service never notifies the session service. This
long-lived per-host loop polls its **unprovisioned** tasks and, when one has acquired a slug,
provisions it — `Provisioner.provision` branches the per-task clone and records the result. Once a
task is provisioned it drops out of the watch set, so the loop stops re-polling it. `provision`
stays idempotent as a safety net for the race between a snapshot and the call. Slug-set is a
one-time transition per task, so the poll is cheap; the interval is the only latency knob (a
long-poll variant can cut it later without changing the direction).

The watch set is supplied by the host; a transient git/REST error on one task is logged and
skipped so it can't stall the others. LLM-free.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Iterable

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.sessionservice.provisioner import Provisioner

_log = logging.getLogger(__name__)

#: Per-task clones root (matches the spawn entrypoint's ``--tasks-root``); each task's clone is
#: ``<tasks_root>/<task_id>`` (ADR 0011).
DEFAULT_TASKS_ROOT = os.path.expanduser("~/.panopticon/tasks")


class ProvisionDaemon:
    """Polls the watched tasks and provisions each once it acquires a slug.

    ``tasks`` yields the ids of this host's **unprovisioned** tasks — those without a branch yet
    (re-read each pass, so a task drops out once provisioned and newly spawned ones appear).
    ``sleep``/``interval`` are injectable so the loop is testable without real waiting.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        provisioner: Provisioner,
        tasks: Callable[[], Iterable[str]],
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
    ) -> None:
        self._client = client
        self._provisioner = provisioner
        self._tasks = tasks
        self._sleep = sleep
        self._interval = interval

    def tick(self) -> list[str]:
        """One pass over the watched tasks; returns the branches provisioned this pass."""
        provisioned: list[str] = []
        for task_id in self._tasks():
            try:
                task = self._client.get_task(task_id)
                if task.get("provisioned"):  # already has a branch — nothing to do
                    continue
                branch = self._provisioner.provision(task)
            except Exception:  # a transient git/REST error on one task must not stall the others
                _log.warning("provisioning pass failed for task %s", task_id, exc_info=True)
                continue
            if branch is not None:
                provisioned.append(branch)
        return provisioned

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Poll until ``until()`` is true (``None`` = forever), provisioning each pass."""
        while not (until and until()):
            self.tick()
            self._sleep(self._interval)


def watched_tasks(client: TaskServiceClient) -> Callable[[], list[str]]:
    """A watch-set provider: this host's **unprovisioned** task ids — those still needing a branch.

    For M1 (single host) that's every not-yet-provisioned task the service knows; a task drops out
    of the set once `Task.provisioned` is true. Scoping to this runner's own tasks (via
    registrations) is an M5 refinement.
    """
    return lambda: [t["id"] for t in client.list_tasks() if not t["provisioned"]]


def run_daemon(
    client: TaskServiceClient,
    *,
    tasks_root: str,
    interval: float = 2.0,
    git: GitClones | None = None,
    until: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Build the provisioner + daemon over this host's tasks and run the loop (ADR 0010/0011)."""
    provisioner = Provisioner(client, clones_root=tasks_root, git=git)
    daemon = ProvisionDaemon(client, provisioner, watched_tasks(client), interval=interval, sleep=sleep)
    daemon.run(until=until)


def main(argv: list[str] | None = None, *, client: TaskServiceClient | None = None) -> None:  # pragma: no cover - thin wiring + endless loop
    """``python -m panopticon.sessionservice.daemon`` — watch this host's tasks and provision them."""
    parser = argparse.ArgumentParser(
        prog="python -m panopticon.sessionservice.daemon",
        description="Observe tasks and provision each once it acquires a slug (ADR 0010/0011).",
    )
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", "http://localhost:8000"),
        help="task service URL to pull task state from",
    )
    parser.add_argument("--tasks-root", default=os.environ.get("PANOPTICON_TASKS_ROOT", DEFAULT_TASKS_ROOT))
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval, seconds")
    args = parser.parse_args(argv)
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    run_daemon(client, tasks_root=args.tasks_root, interval=args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
