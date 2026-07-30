"""Stalled-agent detection & recovery (ADR 0014). `classify_pane_text`/`parse_reset_time` are
pure — tested directly against fixture text. `StallMonitor` is tested with fakes standing in for
LocalRunner/Spawner/the task-service client, driving `tick()` across a fake clock the same way
`test_spawner.py` drives `heal()`'s crash-loop budget. No Docker, no tmux, no LLM."""

from __future__ import annotations

from panopticon.client import JsonObj
from panopticon.sessionservice.local_runner import ProcessSnapshot
from panopticon.sessionservice.stall import (
    StallCause,
    StallMonitor,
    classify_pane_text,
    parse_reset_time,
)

# ---------------------------------------------------------------------------------------------
# classify_pane_text — pure, fixture-driven
# ---------------------------------------------------------------------------------------------

_NOW = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC


def test_classify_recognizes_a_usage_limit_banner_and_parses_the_reset_time() -> None:
    text = "Claude AI usage limit reached. Your limit will reset at 23:00.\n"
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "usage_limit"
    assert cause.reset_at is not None
    assert cause.reset_at > _NOW


def test_classify_recognizes_a_usage_limit_banner_with_meridiem_time() -> None:
    text = "Claude AI usage limit reached. Your limit will reset at 9pm (America/Los_Angeles).\n"
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "usage_limit"
    assert cause.reset_at is not None


def test_classify_recognizes_an_overloaded_error() -> None:
    text = (
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
    )
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "overloaded_error"


def test_classify_recognizes_a_rate_limit_error() -> None:
    text = '{"type":"error","error":{"type":"rate_limit_error","message":"..."}}'
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "rate_limit_error"


def test_classify_recognizes_a_network_error() -> None:
    text = "fetch failed\ncause: Error: ECONNRESET"
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "network_error"


def test_classify_recognizes_a_generic_5xx_server_error() -> None:
    text = "API Error: 503 Service Unavailable"
    cause = classify_pane_text(text, now=_NOW)
    assert cause.kind == "server_error"


def test_classify_falls_back_to_unknown_error_rather_than_none() -> None:
    # The operator's report is explicit: errors are heterogeneous ("other api errors"). Recovery
    # must not require an exhaustive signature list, so unrecognized text still yields a cause.
    cause = classify_pane_text("some completely unrecognized banner\n$ ", now=_NOW)
    assert cause.kind == "unknown_error"
    assert cause.reset_at is None


def test_classify_falls_back_to_unknown_error_on_a_blank_pane() -> None:
    # The reference incident's shape: a stall with no informative text on screen at all (just a
    # bare idle prompt) must still be classifiable, not raise or return nothing.
    cause = classify_pane_text("$ \n", now=_NOW)
    assert cause.kind == "unknown_error"


def test_classify_returns_a_stall_cause_dataclass() -> None:
    assert isinstance(classify_pane_text("anything", now=_NOW), StallCause)


# ---------------------------------------------------------------------------------------------
# parse_reset_time — pure
# ---------------------------------------------------------------------------------------------


def test_parse_reset_time_rolls_over_to_the_next_day_when_already_past() -> None:
    # `_NOW` is 22:13:20 UTC — "9am" has already passed today.
    reset_at = parse_reset_time("9am", now=_NOW)
    assert reset_at is not None
    assert reset_at > _NOW
    # roughly 10h45m away (next day 09:00 from today 22:13), not ~24h+ (would indicate a bug
    # rolling over twice) nor negative/past (would indicate no rollover at all).
    assert 0 < reset_at - _NOW < 24 * 3600


def test_parse_reset_time_handles_24h_clock_with_minutes() -> None:
    reset_at = parse_reset_time("23:45", now=_NOW)
    assert reset_at is not None
    assert reset_at > _NOW


def test_parse_reset_time_returns_none_for_unparsable_text() -> None:
    assert parse_reset_time("sometime soon, who knows", now=_NOW) is None


# ---------------------------------------------------------------------------------------------
# StallMonitor — fakes for LocalRunner/Spawner/the task-service client
# ---------------------------------------------------------------------------------------------


class _FakeRunner:
    """Stands in for LocalRunner: `is_running`/`has_session` are fixed; `transcript_mtime`/
    `process_snapshot`/`pane_text` return whatever the test last set on `*_value` (mutated
    between `tick()` calls, mirroring how a real transcript/pane would change or stay put)."""

    def __init__(self, *, running: bool = True, session: bool = True) -> None:
        self._running = running
        self._session = session
        self.transcript_mtime_value: float | None = None
        self.process_snapshot_value = ProcessSnapshot(claude_present=True, tool_active=False)
        self.pane_text_value: str = ""
        self.sent_keys: list[tuple[str, str]] = []
        self.transcript_calls = 0
        self.process_calls = 0
        self.pane_calls = 0

    def is_running(self, task_id: str) -> bool:
        return self._running

    def has_session(self, task_id: str) -> bool:
        return self._session

    def transcript_mtime(self, task_id: str) -> float | None:
        self.transcript_calls += 1
        return self.transcript_mtime_value

    def process_snapshot(self, task_id: str) -> ProcessSnapshot:
        self.process_calls += 1
        return self.process_snapshot_value

    def pane_text(self, task_id: str, *, lines: int = 200) -> str:
        self.pane_calls += 1
        return self.pane_text_value

    def send_keys(self, task_id: str, text: str) -> None:
        self.sent_keys.append((task_id, text))


class _FakeSpawner:
    """Stands in for Spawner: records `respawn` calls."""

    def __init__(self) -> None:
        self.respawned: list[JsonObj] = []

    def respawn(self, task: JsonObj) -> str:
        self.respawned.append(task)
        return f"panopticon-{task['id']}"


class _FakeClient:
    """Stands in for TaskServiceClient: serves one workflow's execution spec, records reported
    lifecycle phases and clears."""

    def __init__(self, *, runner_type: str = "docker") -> None:
        self._runner_type = runner_type
        self.phases: list[tuple[str, str, str | None]] = []
        self.cleared: list[str] = []

    def workflow_execution(self, name: str) -> JsonObj:
        return {
            "runner_type": self._runner_type,
            "script": "",
            "clone_repo": False,
            "workdir": None,
        }

    def report_lifecycle(
        self, task_id: str, runner_id: str, phase: str, detail: str | None = None
    ) -> JsonObj:
        self.phases.append((task_id, phase, detail))
        return {"id": task_id}

    def clear_lifecycle(self, task_id: str) -> JsonObj:
        self.cleared.append(task_id)
        return {"id": task_id}


def _task(
    task_id: str = "t1",
    *,
    turn: str = "agent",
    state: str = "ITERATING",
    claimed_by: str | None = "host-1",
    workflow: str = "spike",
    updated_at: str | None = None,
) -> JsonObj:
    return {
        "id": task_id,
        "state": state,
        "claimed_by": claimed_by,
        "turn": turn,
        "workflow": workflow,
        "updated_at": updated_at,
    }


def _monitor(
    client: object,
    runner: object,
    spawner: object,
    *,
    clock: dict[str, float],
    idle_seconds: float = 480.0,
    probe_interval: float = 60.0,
    max_recoveries: int = 3,
    retry_text: str = "try again",
) -> StallMonitor:
    return StallMonitor(
        client,  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        spawner,  # type: ignore[arg-type]
        runner_id="host-1",
        now=lambda: clock["t"],
        idle_seconds=idle_seconds,
        probe_interval_seconds=probe_interval,
        max_recoveries=max_recoveries,
        retry_text=retry_text,
    )


_BASE = 1_700_000_000.0


# -- candidate gating: none of these ever touch docker/tmux -------------------------------------


def test_tick_skips_a_task_not_claimed_by_this_runner() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task(claimed_by="other-host"))
    assert runner.transcript_calls == 0


def test_tick_skips_a_terminal_task() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task(state="COMPLETE"))
    assert runner.transcript_calls == 0


def test_tick_skips_when_the_turn_is_user() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task(turn="user"))
    assert runner.transcript_calls == 0


def test_tick_skips_a_shell_workflow() -> None:
    clock = {"t": _BASE}
    client = _FakeClient(runner_type="shell")
    runner, spawner = _FakeRunner(), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task())
    assert runner.transcript_calls == 0


def test_tick_skips_when_the_container_is_not_running() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(running=False), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task())
    assert runner.transcript_calls == 0


def test_tick_skips_when_there_is_no_tmux_session() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(session=False), _FakeSpawner()
    _monitor(client, runner, spawner, clock=clock).tick(_task())
    assert runner.transcript_calls == 0


# -- fresh transcript: no action -----------------------------------------------------------------


def test_tick_does_nothing_when_the_transcript_is_fresh() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 10.0  # well under the idle threshold
    _monitor(client, runner, spawner, clock=clock).tick(_task())
    assert runner.sent_keys == []
    assert spawner.respawned == []
    assert client.phases == []


def test_tick_falls_back_to_updated_at_when_no_transcript_exists_yet() -> None:
    # Very early in a task (before claude's first response) there's no transcript file at all —
    # staleness still has to be measurable, so it falls back to the task's last known activity.
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = None
    task = _task(updated_at="2023-11-14T22:13:10+00:00")  # 10s before `_BASE`
    _monitor(client, runner, spawner, clock=clock).tick(task)
    assert runner.sent_keys == []  # 10s old — nowhere near the idle threshold


def test_tick_does_nothing_when_neither_transcript_nor_updated_at_exist() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = None
    _monitor(client, runner, spawner, clock=clock).tick(_task(updated_at=None))
    assert runner.pane_calls == 0  # never got far enough to even guess at a stall


# -- probe throttling -----------------------------------------------------------------------------


def test_tick_throttles_probes_independent_of_call_frequency() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"]
    monitor = _monitor(client, runner, spawner, clock=clock, probe_interval=60.0)
    task = _task()
    monitor.tick(task)
    monitor.tick(task)  # immediately again — still inside the throttle window
    assert runner.transcript_calls == 1
    clock["t"] += 61.0
    monitor.tick(task)
    assert runner.transcript_calls == 2


# -- suppression guards ----------------------------------------------------------------------------


def test_tick_suppresses_detection_when_a_tool_is_still_active() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0  # stale
    runner.process_snapshot_value = ProcessSnapshot(claude_present=True, tool_active=True)
    _monitor(client, runner, spawner, clock=clock, idle_seconds=480.0).tick(_task())
    assert runner.sent_keys == []
    assert spawner.respawned == []
    assert client.phases == []
    assert runner.pane_calls == 0  # the guard short-circuits before ever reading the pane


def test_tick_establishes_a_pane_baseline_before_acting() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "some error\n$ "
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)  # first stale probe: establishes the baseline, no action yet
    assert runner.sent_keys == []
    assert client.phases == []
    clock["t"] += 61.0
    monitor.tick(task)  # pane unchanged across the interval — now acts
    assert len(runner.sent_keys) == 1


def test_tick_treats_a_changing_pane_as_active_work_not_a_stall() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "generating...\n"
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    runner.pane_text_value = "generating... more tokens streamed\n"
    monitor.tick(task)
    assert runner.sent_keys == []
    assert spawner.respawned == []


# -- recovery: shape A (claude alive) --------------------------------------------------------------


def test_tick_recovers_shape_a_with_send_keys_and_reports_stalled() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "some idling banner\n$ "
    runner.process_snapshot_value = ProcessSnapshot(claude_present=True, tool_active=False)
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)  # acts
    assert runner.sent_keys == [("t1", "try again")]
    assert spawner.respawned == []
    task_id, phase, detail = client.phases[-1]
    assert task_id == "t1"
    assert phase == "stalled"
    assert detail is not None
    assert "auto-retry 1/3" in detail
    assert "unknown_error" in detail


# -- recovery: shape B (claude gone) ----------------------------------------------------------------


def test_tick_recovers_shape_b_via_respawn_when_claude_is_absent() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "$ \n"
    runner.process_snapshot_value = ProcessSnapshot(claude_present=False, tool_active=False)
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)
    assert spawner.respawned == [task]
    assert runner.sent_keys == []
    _task_id, phase, detail = client.phases[-1]
    assert phase == "stalled"
    assert detail is not None
    assert "auto-retry 1/3" in detail


# -- backoff between attempts ------------------------------------------------------------------------


def test_tick_backs_off_between_recovery_attempts_then_retries_after_the_window() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0  # fixed — the transcript never moves
    runner.pane_text_value = "stuck\n$ "
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()

    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)  # recovery #1 — backoff(1) = 120s
    assert len(runner.sent_keys) == 1

    clock["t"] += 61.0  # 61s since the last action — still inside the 120s backoff
    monitor.tick(task)
    assert len(runner.sent_keys) == 1  # no new attempt yet

    clock["t"] += 61.0  # 122s since the last action — past the 120s backoff
    monitor.tick(task)
    assert len(runner.sent_keys) == 2  # recovery #2


# -- recovery cap -------------------------------------------------------------------------------------


def test_tick_caps_recoveries_then_surfaces_loudly() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "stuck\n$ "
    monitor = _monitor(
        client,
        runner,
        spawner,
        clock=clock,
        idle_seconds=480.0,
        probe_interval=60.0,
        max_recoveries=1,
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)  # recovery #1 (the only one allowed)
    assert len(runner.sent_keys) == 1

    clock["t"] += 61.0
    monitor.tick(task)  # the action reset the pane baseline — this re-establishes it, no action
    assert len(runner.sent_keys) == 1

    clock["t"] += 200.0  # past backoff(1) = 120s
    monitor.tick(task)  # cap already reached — surfaced, not attempted again
    assert len(runner.sent_keys) == 1
    _task_id, phase, detail = client.phases[-1]
    assert phase == "stalled"
    assert detail is not None
    assert "1/1 auto-recoveries exhausted" in detail
    assert "needs manual bump" in detail


# -- manual bump / organic recovery resets everything ---------------------------------------------


def test_tick_fully_resets_once_the_transcript_resumes_growing() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "stuck\n$ "
    monitor = _monitor(
        client,
        runner,
        spawner,
        clock=clock,
        idle_seconds=480.0,
        probe_interval=60.0,
        max_recoveries=1,
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)  # recovery #1 — reports stalled
    assert client.phases[-1][1] == "stalled"

    clock["t"] += 61.0
    runner.transcript_mtime_value = clock["t"]  # a bump (manual or ours) revived it
    monitor.tick(task)
    assert client.cleared == ["t1"]  # the STALLED report is dropped

    # a *fresh* stall afterwards gets a full new recovery budget, not "already exhausted"
    clock["t"] += 700.0
    runner.transcript_mtime_value = clock["t"] - 600.0
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)
    assert len(runner.sent_keys) == 2  # succeeded again — not treated as still-exhausted


def test_tick_drops_tracking_when_the_task_stops_being_a_candidate() -> None:
    # e.g. released (a manual `R` respawn) mid-stall — the task service already clears the
    # lifecycle report itself on release/reclaim; we just need to stop tracking it locally.
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "stuck\n$ "
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)
    clock["t"] += 61.0
    monitor.tick(task)  # now flagged, recovery_count == 1
    assert len(runner.sent_keys) == 1

    clock["t"] += 700.0
    monitor.tick(_task(claimed_by=None))  # released
    assert len(runner.sent_keys) == 1  # nothing further attempted

    # re-claimed and stalled again later — starts with a clean budget, not "already at 1/3"
    clock["t"] += 61.0
    runner.transcript_mtime_value = clock["t"] - 600.0
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)
    assert len(runner.sent_keys) == 2


# -- usage-limit scheduling -------------------------------------------------------------------------


def test_tick_schedules_a_usage_limit_retry_instead_of_acting_immediately() -> None:
    clock = {"t": _BASE}
    client, runner, spawner = _FakeClient(), _FakeRunner(), _FakeSpawner()
    runner.transcript_mtime_value = clock["t"] - 600.0
    runner.pane_text_value = "Claude AI usage limit reached. Your limit will reset at 23:00.\n"
    monitor = _monitor(
        client, runner, spawner, clock=clock, idle_seconds=480.0, probe_interval=60.0
    )
    task = _task()
    monitor.tick(task)  # baseline
    clock["t"] += 61.0
    monitor.tick(task)  # detects usage_limit — schedules, doesn't act yet
    assert runner.sent_keys == []
    assert spawner.respawned == []
    _task_id, phase, detail = client.phases[-1]
    assert phase == "stalled"
    assert detail is not None
    assert "usage limit: retrying at 23:00" in detail

    # still inside the scheduled window — repeated ticks change nothing (and don't re-report)
    clock["t"] += 61.0
    monitor.tick(task)
    assert runner.sent_keys == []
    assert len(client.phases) == 1
