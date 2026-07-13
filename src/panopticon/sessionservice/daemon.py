"""The session service's observe-and-provision loop (ADR 0010/0011).

Coordination is **pull, not push**: the session service drives the task service, not the other way
round — but the pull now **blocks until a change** instead of re-polling on an interval. This
long-lived per-host loop parks on the task service's change feed (`GET /tasks?wait=…&since=…`); when
any task changes it wakes with the fresh snapshot and provisions whatever became slugged —
`Provisioner.provision` branches the per-task clone and records the result. Once a task is
provisioned it drops out of the watch set (it's filtered out of the woken snapshot), so the loop
stops considering it. `provision` stays idempotent as a safety net for the race between a snapshot
and the call. The long-poll `wait` is the only latency knob: a quiet feed costs no requests, and a
slug appears the moment the agent sets it (the slug-set itself is the change that wakes us).

The slug is set **by the container** (`container/entrypoint.py`) via the task service, so the task
service is the event producer this loop waits on — a clean block-until-change fit. A transient
git/REST error on one task is logged and skipped so it can't stall the others; a whole-pass failure
(the feed request itself raising) is logged and retried after `interval`, so a blip never kills the
loop. LLM-free.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Iterable

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.dirs import TASKS_DIR
from panopticon.core.git import GitClones
from panopticon.sessionservice.provisioner import Provisioner

_log = logging.getLogger(__name__)


class ProvisionDaemon:
    """Blocks on the task-service change feed and provisions each task once it acquires a slug.

    ``interval`` is the long-poll ``wait`` (how long the feed request parks before returning a quiet
    snapshot); ``sleep`` is the backoff applied only when the feed request itself raises. Both are
    injectable so the loop is testable without real waiting.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        provisioner: Provisioner,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
    ) -> None:
        self._client = client
        self._provisioner = provisioner
        self._sleep = sleep
        self._interval = interval

    def provision(self, tasks: Iterable[JsonObj]) -> list[str]:
        """Provision each watched (unprovisioned) task in a snapshot; return the branches created.

        ``provision`` is idempotent — it no-ops a task with no slug yet — so the unprovisioned ones
        that aren't slugged simply yield nothing; a transient git/REST error on one task is logged
        and skipped so it can't stall the others.
        """
        provisioned: list[str] = []
        for task in watched_tasks(tasks):
            try:
                branch = self._provisioner.provision(task)
            except Exception:  # a transient git/REST error on one task must not stall the others
                _log.warning("provisioning pass failed for task %s", task.get("id"), exc_info=True)
                continue
            if branch is not None:
                provisioned.append(branch)
        return provisioned

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Block on the change feed until ``until()`` is true (``None`` = forever).

        Each pass parks on ``list_tasks_versioned(wait=interval)`` until a task changes past the last
        version we saw (or the wait elapses), then provisions the woken snapshot and feeds the
        returned version back as ``since`` to wait for the *next* change. A whole-pass failure — the
        feed request raising on a service blip or before the service is listening — is logged and
        retried after ``interval`` (`sleep`), so a transient error never kills the long-lived daemon.
        """
        since = 0
        while not (until and until()):
            try:
                tasks, since = self._client.list_tasks_versioned(since=since, wait=self._interval)
            except Exception:
                _log.warning("change-feed request failed; retrying next interval", exc_info=True)
                self._sleep(self._interval)
                continue
            self.provision(tasks)


def watched_tasks(tasks: Iterable[JsonObj]) -> list[JsonObj]:
    """The **unprovisioned** tasks in a snapshot — those still needing a branch.

    A task drops out of the set once `Task.provisioned` is true, so filtering the woken change-feed
    snapshot replaces re-querying the service for the watch set. For M1 (single host) the snapshot is
    every task the service knows; scoping to this runner's own tasks is an M5 refinement.
    """
    return [t for t in tasks if not t["provisioned"]]


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
    daemon = ProvisionDaemon(client, provisioner, interval=interval, sleep=sleep)
    daemon.run(until=until)


def main(
    argv: list[str] | None = None, *, client: TaskServiceClient | None = None
) -> None:  # pragma: no cover - thin wiring + endless loop
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
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval, seconds")
    args = parser.parse_args(argv)
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    run_daemon(client, tasks_root=TASKS_DIR, interval=args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
