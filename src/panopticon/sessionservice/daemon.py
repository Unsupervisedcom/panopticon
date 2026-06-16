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

import logging
import time
from collections.abc import Callable, Iterable

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.provisioner import Provisioner

_log = logging.getLogger(__name__)


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
