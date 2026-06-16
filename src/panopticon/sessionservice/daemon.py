"""The session service's observe-and-provision loop (ADR 0010).

Coordination is **pull, not push**: the task service never notifies the session service. This
long-lived per-host loop polls the tasks this host is running and, when one has acquired a slug,
provisions it (`Provisioner` → clone + worktree + record). `Provisioner.provision` is idempotent,
so re-seeing an already-provisioned task is a no-op — the loop just calls it on every watched task
each pass. Slug-set is a one-time transition per task, so the poll is cheap; the interval is the
only latency knob (a long-poll variant can cut it later without changing the direction).

The set of watched tasks is supplied by the host (the tasks it has spawned); a transient git/REST
error on one task is logged and skipped so it can't stall the others. LLM-free.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from panopticon.client import TaskServiceClient
from panopticon.core.git import Worktree
from panopticon.sessionservice.provisioner import Provisioner

_log = logging.getLogger(__name__)


class ProvisionDaemon:
    """Polls the watched tasks and provisions each once it acquires a slug.

    ``tasks`` yields the task ids this host is running (re-read each pass, so the host can add or
    retire tasks between passes). ``sleep``/``interval`` are injectable so the loop is testable
    without real waiting.
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

    def tick(self) -> list[Worktree]:
        """One pass over the watched tasks; returns the worktrees provisioned this pass."""
        provisioned: list[Worktree] = []
        for task_id in self._tasks():
            try:
                worktree = self._provisioner.provision(self._client.get_task(task_id))
            except Exception:  # a transient git/REST error on one task must not stall the others
                _log.warning("provisioning pass failed for task %s", task_id, exc_info=True)
                continue
            if worktree is not None:
                provisioned.append(worktree)
        return provisioned

    def run(self, *, until: Callable[[], bool] | None = None) -> None:
        """Poll until ``until()`` is true (``None`` = forever), provisioning each pass."""
        while not (until and until()):
            self.tick()
            self._sleep(self._interval)
