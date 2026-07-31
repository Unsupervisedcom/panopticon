"""Stalled-agent detection & auto-recovery (ADR 0014).

The host daemon's answer to a claude agent that's gone silent mid-turn (dropped task
f088043b's motivating report): a transient API failure that leaves the CLI idling at its own
prompt (shape A — claude's process is alive, just stuck), or claude's process genuinely gone
from an otherwise-live container (shape B). Both leave ``Task.turn`` reading ``"agent"``
forever, since the turn-flip contract is purely event-driven (``container/hooks.py``) — nothing
fires when the agent hangs or dies, so nothing here calls an LLM either: this module only
inspects processes/logs and drives ``docker``/``tmux``, exactly like the runner/spawner code it
sits beside (the determinism invariant).

**Detection is staleness-driven, not a symmetric multi-signal vote.** Verified empirically
across 60 sampled task volumes: claude never writes API-error banners to its transcript, so
there is nothing to look *for* there — only whether it's *still growing*. The transcript's age
(while ``turn == "agent"``) is the primary trigger; the container's process tree and the tmux
pane's content are **suppression guards** (an in-flight tool call, or a still-streaming
generation, explain a transcript gap without a stall) rather than peer signals. Once a candidate
survives both guards for a full idle window, the tmux pane — the *only* place claude's error
text exists at all — is classified by :func:`classify_pane_text` to pick a recovery.

Built against a real fleet incident (23:30-03:38 UTC, four concurrent tasks stalled ~4h each,
two revived only by a manual operator message): task `8fa7de51`'s last transcript record was a
``tool_result`` at 23:37, with no further record until 03:38 after a manual bump — the tool had
already finished, so the stall was in the *next* LLM call, not a still-running tool. That shape
is what the tool-active guard is built to *not* suppress.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.models import LifecyclePhase
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.executions import WorkflowExecutions
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner

_log = logging.getLogger(__name__)

#: Silence on the transcript (while turn == "agent") required before a task is even considered a
#: stall candidate. Conservative by design — the reference incident this was built against ran
#: ~4h before a manual bump; even this default would have cut it to a small fraction of that.
DEFAULT_IDLE_SECONDS = 8 * 60
#: How often an individual candidate is actually re-probed (each probe costs a `docker exec` +
#: a `tmux capture-pane`) — independent of the host loop's own ~2s tick.
DEFAULT_PROBE_INTERVAL_SECONDS = 60.0
#: Cap on consecutive auto-recoveries for one continuous stall before we stop and surface it
#: loudly instead of looping. Resets the moment the transcript resumes growing (real progress),
#: not on a timer — see `_TaskState`/`StallMonitor.tick`.
DEFAULT_MAX_RECOVERIES = 3
#: What's typed into the pane for a shape A (process alive) nudge — the direct automation of the
#: operator's own manual "try again" bump.
DEFAULT_RETRY_TEXT = "try again"


@dataclass(frozen=True)
class StallCause:
    """The classified reason a stalled agent's pane shows what it shows.

    ``kind`` is a short machine-readable tag (``usage_limit``, ``overloaded_error``,
    ``rate_limit_error``, ``network_error``, ``server_error``, or the lenient fallback
    ``unknown_error``) — logged and folded into the surfaced lifecycle detail so stall time can
    later be attributed by cause (the task time-profiler, `profiler/`, doesn't cross-reference
    this yet — see `docs/design/BACKLOG.md`). ``reset_at`` is only ever set for ``usage_limit``
    when a reset time was confidently parsed (epoch seconds); ``excerpt`` is the matched snippet
    (or, for `unknown_error`, the pane's tail), trimmed for logging.
    """

    kind: str
    reset_at: float | None = None
    excerpt: str = ""


_USAGE_LIMIT_RE = re.compile(
    r"usage limit reached.{0,80}?reset(?:s)?\s*(?:at)?\s*([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?)",
    re.IGNORECASE | re.DOTALL,
)
_OVERLOADED_RE = re.compile(
    r"overloaded_error|\b529\b|server is overloaded|service is temporarily overloaded",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(r"rate_limit_error|\b429\b|too many requests", re.IGNORECASE)
_NETWORK_RE = re.compile(
    r"econnreset|econnrefused|etimedout|network error|fetch failed|socket hang up|"
    r"request timed out|connection (?:error|reset|refused)",
    re.IGNORECASE,
)
_SERVER_ERROR_RE = re.compile(r"\b5\d{2}\b|internal_server_error|\bapi error\b", re.IGNORECASE)
_TIME_RE = re.compile(r"([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", re.IGNORECASE)


def classify_pane_text(text: str, *, now: float) -> StallCause:
    """Classify a captured tmux pane's tail against known claude CLI failure signatures.

    This is the **only** source of error-cause text available anywhere (the transcript carries
    none — see the module docstring), so it's deliberately lenient: text that matches nothing
    known still yields a cause (``unknown_error``), never ``None`` — recovery must not require an
    exhaustive signature list, or it would silently do nothing for exactly the heterogeneous
    "other api errors" the operator reported, and for the reference incident's two tasks that
    needed a manual bump. ``now`` (epoch seconds) anchors :func:`parse_reset_time` for the
    ``usage_limit`` case; pure otherwise, so this is unit-tested directly against fixture text."""
    if match := _USAGE_LIMIT_RE.search(text):
        reset_at = parse_reset_time(match.group(1), now=now)
        return StallCause(kind="usage_limit", reset_at=reset_at, excerpt=match.group(0)[:200])
    if match := _OVERLOADED_RE.search(text):
        return StallCause(kind="overloaded_error", excerpt=match.group(0)[:200])
    if match := _RATE_LIMIT_RE.search(text):
        return StallCause(kind="rate_limit_error", excerpt=match.group(0)[:200])
    if match := _NETWORK_RE.search(text):
        return StallCause(kind="network_error", excerpt=match.group(0)[:200])
    if match := _SERVER_ERROR_RE.search(text):
        return StallCause(kind="server_error", excerpt=match.group(0)[:200])
    return StallCause(kind="unknown_error", excerpt=text.strip()[-200:])


def parse_reset_time(text: str, *, now: float) -> float | None:
    """Best-effort parse of a printed reset time (``"9pm"``, ``"21:00"``, ``"9:30 pm"``) into an
    epoch timestamp on the same UTC day as ``now``, rolled to the next day if that time has
    already passed today. claude's exact wording here isn't pinned down by any spec available at
    write time (see ADR 0014's open question — no real captured examples yet), so this is
    deliberately narrow and timezone-naive (assumes UTC): anything it can't confidently parse
    returns ``None``, and the caller falls back to ordinary backoff instead of a scheduled retry.
    """
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = datetime.fromtimestamp(now, tz=UTC).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if base.timestamp() <= now:
        base = base + timedelta(days=1)
    return base.timestamp()


def _parse_iso(value: str | None) -> float | None:
    """``Task.updated_at``/similar ISO-8601 strings → epoch seconds, or ``None`` for anything
    absent/unparsable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _format_hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%H:%M")


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff between recovery attempts: 2, 4, 8, capped at 16 minutes."""
    return min(2.0**attempt, 16.0) * 60.0


@dataclass
class _TaskState:
    """Per-task bookkeeping between :meth:`StallMonitor.tick` calls. Dropped (not decayed on a
    timer) the moment a task is no longer a stall candidate or is observed to have resumed real
    progress — a stricter reset than `Spawner`'s respawn survivor-window, and simpler to justify
    here: unlike "the container came up" (which doesn't yet prove the *agent* is healthy), "the
    transcript resumed growing" is direct proof of it, so there's nothing left to decay."""

    last_probed_at: float = 0.0
    last_pane_text: str | None = None
    recovery_count: int = 0
    last_recovery_at: float = 0.0
    next_action_at: float = 0.0
    usage_limit_scheduled: bool = False
    reported: bool = False


class StallMonitor:
    """Detects and recovers stalled agents in claimed, live, ``turn == "agent"`` tasks.

    A `Spawner`-sibling collaborator, called once per task per host-daemon pass
    (:meth:`tick`, wired into ``HostDaemon.tick`` alongside `Spawner`'s own per-task calls) —
    self-gating and internally throttled, so calling it on every task every pass is safe and
    cheap. Shell tasks are skipped (no claude agent runs there, same reason `Spawner._is_orphan`
    skips them).

    ``now`` is **wall-clock** (epoch seconds, default :func:`time.time`) rather than the
    monotonic clock `Spawner` injects for its own budget tracking — unlike a respawn budget,
    this class compares its clock directly against the transcript's real mtime and a parsed
    usage-limit reset time, both of which are wall-clock timestamps.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        runner: LocalRunner,
        spawner: Spawner,
        *,
        runner_id: str,
        executions: WorkflowExecutions | None = None,
        now: Callable[[], float] = time.time,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        max_recoveries: int = DEFAULT_MAX_RECOVERIES,
        retry_text: str = DEFAULT_RETRY_TEXT,
    ) -> None:
        self._client = client
        self._runner = runner
        self._spawner = spawner
        self._runner_id = runner_id
        self._executions = executions or WorkflowExecutions(client)
        self._now = now
        self._idle_seconds = idle_seconds
        self._probe_interval = probe_interval_seconds
        self._max_recoveries = max_recoveries
        self._retry_text = retry_text
        #: task_id → in-progress tracking. Lazily pruned (dropped whenever a task stops being a
        #: candidate or is observed to have recovered) — never explicitly swept, mirroring
        #: `Spawner._respawns`'s own staleness tolerance.
        self._states: dict[str, _TaskState] = {}

    def _is_candidate(self, task: JsonObj) -> bool:
        """Claimed by us, non-terminal, not a shell workflow, the agent's turn, and actually
        live (container + tmux session both up) — i.e. exactly the tasks that read healthy today
        but might not be. Checked against the runner directly (not the composed
        ``container_status``), since once we report ``STALLED`` that composed status stops
        reading ``live`` — using it here would make us stop watching the very task we flagged."""
        if task.get("claimed_by") != self._runner_id or task["state"] in TERMINAL_LABELS:
            return False
        if task.get("turn") != "agent":
            return False
        if self._executions.is_shell(task.get("workflow")):
            return False
        task_id = task["id"]
        return self._runner.is_running(task_id) and self._runner.has_session(task_id)

    def tick(self, task: JsonObj) -> None:
        """One task's worth of work for one host-daemon pass. Self-throttled independently of
        the host loop's own cadence (see :data:`DEFAULT_PROBE_INTERVAL_SECONDS`) — most calls
        return immediately without touching docker/tmux."""
        task_id = task["id"]
        if not self._is_candidate(task):
            self._states.pop(task_id, None)
            return
        state = self._states.setdefault(task_id, _TaskState())
        now = self._now()
        if now - state.last_probed_at < self._probe_interval:
            return
        state.last_probed_at = now

        mtime = self._runner.transcript_mtime(task_id)
        if mtime is None:
            mtime = _parse_iso(task.get("updated_at"))
        if mtime is None:
            return  # nothing to measure staleness against yet (very early in the task)

        age = now - mtime
        if age < self._idle_seconds:
            self._resolve(task_id, state)
            return

        snapshot = self._runner.process_snapshot(task_id)
        if snapshot.tool_active:
            # a tool call is genuinely in flight — the transcript gap is explained, not a stall
            self._resolve(task_id, state)
            return

        pane = self._runner.pane_text(task_id)
        if state.last_pane_text is None:
            state.last_pane_text = pane  # establish a baseline; act on the next probe if frozen
            return
        if pane != state.last_pane_text:
            # still streaming (or otherwise producing visible output) — active, not stalled
            state.last_pane_text = pane
            self._resolve(task_id, state)
            return

        if now < state.next_action_at:
            return  # waiting out a backoff, or a scheduled usage-limit retry

        cause = classify_pane_text(pane, now=now)
        if (
            cause.kind == "usage_limit"
            and cause.reset_at is not None
            and cause.reset_at > now
            and not state.usage_limit_scheduled
        ):
            state.next_action_at = cause.reset_at
            state.usage_limit_scheduled = True
            self._report(task_id, state, f"usage limit: retrying at {_format_hhmm(cause.reset_at)}")
            _log.warning(
                "task %s: stall detected (cause=usage_limit) — scheduling retry at %s",
                task_id,
                _format_hhmm(cause.reset_at),
            )
            return
        state.usage_limit_scheduled = False

        if state.recovery_count >= self._max_recoveries:
            self._report(
                task_id,
                state,
                f"stalled: {state.recovery_count}/{self._max_recoveries} auto-recoveries "
                f"exhausted (cause={cause.kind}) — needs manual bump",
            )
            _log.warning(
                "task %s: stall recovery cap reached (%d/%d, cause=%s) — needs manual bump",
                task_id,
                state.recovery_count,
                self._max_recoveries,
                cause.kind,
            )
            return

        state.recovery_count += 1
        state.last_recovery_at = now
        state.next_action_at = now + _backoff_seconds(state.recovery_count)
        # The action itself changes the pane — `send_keys` types visibly (echoed by the pty, or by
        # claude's own input rendering) and a respawn tears down this pane entirely — so the *next*
        # probe must not compare against the pre-action baseline (it would see "the pane changed"
        # and wrongly resolve a stall that was never actually fixed, after only one attempt). Force
        # a fresh baseline; genuine continued silence still resolves to stalled on the probe after.
        state.last_pane_text = None
        if snapshot.claude_present:
            self._runner.send_keys(task_id, self._retry_text)
            action = "send-keys retry"
        else:
            self._spawner.respawn(task)
            action = "respawn (--continue)"
        self._report(
            task_id,
            state,
            f"api-error: auto-retry {state.recovery_count}/{self._max_recoveries} "
            f"(cause={cause.kind})",
        )
        _log.warning(
            "task %s: stall detected (cause=%s, claude_process=%s) — recovery %d/%d: %s",
            task_id,
            cause.kind,
            snapshot.claude_present,
            state.recovery_count,
            self._max_recoveries,
            action,
        )

    def _report(self, task_id: str, state: _TaskState, detail: str) -> None:
        state.reported = True
        with contextlib.suppress(httpx.HTTPError):
            self._client.report_lifecycle(
                task_id, self._runner_id, LifecyclePhase.STALLED.value, detail
            )

    def _clear(self, task_id: str, state: _TaskState) -> None:
        """Drop our reported ``STALLED`` phase — best-effort, and only when we're the one who set
        it (a task that was never flagged has nothing to clear)."""
        if state.reported:
            with contextlib.suppress(httpx.HTTPError):
                self._client.clear_lifecycle(task_id)

    def _resolve(self, task_id: str, state: _TaskState) -> None:
        """The task looks healthy again — a fresh transcript, an explained gap (a live tool
        call), or renewed pane output. Clears any reported ``STALLED`` phase and drops the
        stall-specific bookkeeping (recovery budget, backoff, pane baseline) so a *later* stall
        starts with a full fresh budget rather than reading as already-exhausted (requirement 4:
        a manual bump — or the transcript simply resuming on its own — must reset the counter).
        Deliberately keeps ``last_probed_at``, though: this task is still a live candidate we'll
        keep watching, and dropping it here would make every ordinary "still healthy" probe
        cost a fresh `docker exec`/`tmux capture-pane` on the *next* host-daemon tick (~2s later)
        instead of waiting out the probe interval — exactly the per-probe cost throttling exists
        to avoid."""
        self._clear(task_id, state)
        state.last_pane_text = None
        state.recovery_count = 0
        state.last_recovery_at = 0.0
        state.next_action_at = 0.0
        state.usage_limit_scheduled = False
        state.reported = False
