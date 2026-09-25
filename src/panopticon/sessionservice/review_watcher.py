"""Derive ``Task.waiting_on`` from the forge (ADR 0008's observe-and-record shape).

The sibling of :class:`~panopticon.sessionservice.provisioner.Provisioner` and
:class:`~panopticon.sessionservice.ask_worker.AskWorker` for the triage side. The task service
records *why* a task is parked on a third party but cannot work it out — it has no forge access and
stays network-free by design. The session service runs where ``gh`` is authenticated, so it owns
the derivation: each pass it reads the PR of any task that has one and reports what it finds.

**Derived, not declared.** Nothing has to remember to set this and nothing has to remember to clear
it: when the PR is approved or the checks go green, the next pass reports ``None`` and the marker
disappears on its own. That is the whole reason this is a watcher rather than an agent skill — a
skill that forgets to clear leaves a task looking parked forever, which is worse than no marker at
all, because the operator learns to distrust it.

LLM-free. ``run`` is injectable so the emitted ``gh`` commands are unit-testable without a forge.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import CommandRunner, _subprocess_run
from panopticon.core.models import WaitingOn
from panopticon.core.state import TERMINAL_LABELS

_log = logging.getLogger(__name__)

#: Seconds before a task's PR is re-read. The host daemon wakes on the change feed, not a timer, so
#: a busy fleet can tick many times a second — without this, each tick would be one `gh` call per
#: task with a PR. Review state changes on human timescales; a minute of staleness costs nothing.
POLL_INTERVAL_SECONDS = 60.0

#: Check states that mean CI hasn't finished. Anything else (SUCCESS, FAILURE, …) has concluded —
#: a *failing* check is not "waiting on CI", it's work for whoever owns the task.
_PENDING_CHECK_STATES = frozenset({"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"})


class ReviewWatcher:
    """Reads each task's PR and records why it's parked, or that it no longer is."""

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        run: CommandRunner = _subprocess_run,
        now: Callable[[], float] = time.monotonic,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._run = run
        self._now = now
        self._poll_interval = poll_interval
        #: task id → monotonic time of its last successful read, for the throttle above.
        self._last_polled: dict[str, float] = {}

    def observe(self, task: JsonObj) -> WaitingOn | None:
        """Record why ``task`` is parked on a third party, returning what was recorded.

        Self-gating, so the host daemon can call it on every task each pass: a task with no PR, a
        terminal one, or one polled within :data:`POLL_INTERVAL_SECONDS` is skipped and returns the
        value already on the task.

        A read failure is **not** treated as "nothing to wait on" — the previously recorded value is
        left alone. Reporting ``None`` because ``gh`` was rate-limited would quietly mark a parked
        task as actionable, which is the exact error this feature exists to prevent.
        """
        task_id = task["id"]
        current = task.get("waiting_on")
        if not task.get("url") or task["state"] in TERMINAL_LABELS:
            return self._as_enum(current)
        if task.get("paused"):
            return self._as_enum(current)  # no container, no triage value — don't spend the call
        last = self._last_polled.get(task_id)
        if last is not None and self._now() - last < self._poll_interval:
            return self._as_enum(current)

        pr = self._read_pr(str(task["url"]))
        if pr is None:
            return self._as_enum(current)  # unreadable — keep what we had, don't guess
        self._last_polled[task_id] = self._now()

        waiting_on = self._derive(pr)
        if waiting_on != self._as_enum(current):
            self._client.set_waiting_on(task_id, waiting_on.value if waiting_on else None)
            _log.info(
                "task %s: waiting_on %s → %s",
                task_id,
                current,
                waiting_on.value if waiting_on else None,
            )
        return waiting_on

    @staticmethod
    def _as_enum(value: object) -> WaitingOn | None:
        """The task's recorded value as an enum. Tolerant: an unrecognized string (a newer runner
        wrote a reason this one doesn't know) reads as ``None`` rather than raising mid-pass."""
        if not isinstance(value, str):
            return None
        try:
            return WaitingOn(value)
        except ValueError:
            return None

    def _read_pr(self, url: str) -> JsonObj | None:
        """The PR's review + check state, or ``None`` if it can't be read.

        Every failure mode lands here and is swallowed deliberately: `gh` absent, unauthenticated,
        rate-limited, the URL not being a PR, a network blip. None of those should stall a host
        pass, and none of them are evidence about the PR.
        """
        try:
            out = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    url,
                    "--json",
                    "state,reviewDecision,statusCheckRollup",
                ],
                check=False,
            )
        except Exception:
            _log.debug("gh pr view failed for %s", url, exc_info=True)
            return None
        try:
            parsed = json.loads(out)
        except (ValueError, TypeError):
            return None  # `gh` printed an error rather than JSON (not a PR, no auth, …)
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _derive(pr: JsonObj) -> WaitingOn | None:
        """Fold the PR's state into a reason, or ``None`` when the ball is ours.

        Precedence is review-before-CI, deliberately. Both mean "not yours", but a required review
        sits for days while checks resolve in minutes, so the review is the more useful thing to
        show. Two states that look like waiting but aren't:

        * ``CHANGES_REQUESTED`` — the reviewer has acted and handed it *back*; that is work.
        * a failing (not pending) check — also work, for the same reason.
        """
        if pr.get("state") != "OPEN":
            return None  # merged or closed — nothing left to wait for
        if pr.get("reviewDecision") == "REVIEW_REQUIRED":
            return WaitingOn.EXTERNAL_REVIEW
        rollup = pr.get("statusCheckRollup")
        if isinstance(rollup, Sequence) and not isinstance(rollup, str | bytes):
            for check in rollup:
                if not isinstance(check, dict):
                    continue
                # `status` is the workflow-run field; `state` the commit-status one. A rollup mixes
                # both kinds, so a check is pending if *either* says so.
                if (
                    str(check.get("status") or "").upper() in _PENDING_CHECK_STATES
                    or str(check.get("state") or "").upper() in _PENDING_CHECK_STATES
                ):
                    return WaitingOn.CI
        return None
