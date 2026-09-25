"""Deriving `Task.waiting_on` from the forge: the gates, the derivation truth table, the throttle,
and the failure mode that matters most (an unreadable PR must not look actionable). Fake command
runner — no `gh`, no network, no LLM."""

from __future__ import annotations

import json

from panopticon.client import JsonObj
from panopticon.core.models import WaitingOn
from panopticon.sessionservice.review_watcher import ReviewWatcher


class _FakeClient:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str | None]] = []

    def set_waiting_on(self, task_id: str, waiting_on: str | None) -> JsonObj:
        self.recorded.append((task_id, waiting_on))
        return {"id": task_id}


def _runner(payload: object, *, calls: list[list[str]] | None = None):
    """A fake `gh` that returns ``payload`` as JSON (or a raw string verbatim, to stand in for an
    error message rather than JSON)."""

    def run(args, *, check: bool = True) -> str:
        if calls is not None:
            calls.append(list(args))
        return payload if isinstance(payload, str) else json.dumps(payload)

    return run


def _task(**over: object) -> JsonObj:
    base: JsonObj = {
        "id": "t1",
        "state": "REVIEW",
        "url": "https://github.com/acme/widgets/pull/7",
        "waiting_on": None,
    }
    base.update(over)
    return base


_OPEN_REVIEW_REQUIRED = {
    "state": "OPEN",
    "reviewDecision": "REVIEW_REQUIRED",
    "statusCheckRollup": [],
}


# -- derivation ---------------------------------------------------------------------


def test_open_pr_awaiting_review_is_external_review() -> None:
    client = _FakeClient()
    w = ReviewWatcher(client, run=_runner(_OPEN_REVIEW_REQUIRED))  # type: ignore[arg-type]
    assert w.observe(_task()) is WaitingOn.EXTERNAL_REVIEW
    assert client.recorded == [("t1", "external-review")]


def test_pending_checks_are_ci() -> None:
    client = _FakeClient()
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"status": "COMPLETED", "state": "SUCCESS"},
            {"status": "IN_PROGRESS"},
        ],
    }
    w = ReviewWatcher(client, run=_runner(pr))  # type: ignore[arg-type]
    assert w.observe(_task()) is WaitingOn.CI
    assert client.recorded == [("t1", "ci")]


def test_review_takes_precedence_over_ci() -> None:
    # Both mean "not yours", but a required review outlives a check run — show the longer wait.
    client = _FakeClient()
    pr = {
        "state": "OPEN",
        "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [{"status": "IN_PROGRESS"}],
    }
    w = ReviewWatcher(client, run=_runner(pr))  # type: ignore[arg-type]
    assert w.observe(_task()) is WaitingOn.EXTERNAL_REVIEW


def test_changes_requested_is_not_waiting() -> None:
    # The reviewer acted and handed it back. That's work, not a wait — the most important
    # false-positive to avoid, since it's the case where the ball really is ours.
    client = _FakeClient()
    pr = {"state": "OPEN", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": []}
    w = ReviewWatcher(client, run=_runner(pr))  # type: ignore[arg-type]
    assert w.observe(_task()) is None


def test_failing_check_is_not_waiting_on_ci() -> None:
    # A concluded-but-red check is work for us; only an unfinished one is a wait.
    client = _FakeClient()
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE", "state": "FAILURE"}],
    }
    w = ReviewWatcher(client, run=_runner(pr))  # type: ignore[arg-type]
    assert w.observe(_task()) is None


def test_merged_pr_clears_the_marker() -> None:
    # Self-clearing is the reason this is derived rather than agent-declared.
    client = _FakeClient()
    pr = {"state": "MERGED", "reviewDecision": "APPROVED", "statusCheckRollup": []}
    w = ReviewWatcher(client, run=_runner(pr))  # type: ignore[arg-type]
    assert w.observe(_task(waiting_on="external-review")) is None
    assert client.recorded == [("t1", None)]


# -- gates + failure modes ----------------------------------------------------------


def test_unreadable_pr_leaves_the_existing_value_alone() -> None:
    # The failure that would defeat the feature: a rate-limited `gh` must not silently mark a
    # parked task actionable. Keep what we had; report nothing.
    client = _FakeClient()
    w = ReviewWatcher(client, run=_runner("gh: could not determine base repo"))  # type: ignore[arg-type]
    assert w.observe(_task(waiting_on="external-review")) is WaitingOn.EXTERNAL_REVIEW
    assert client.recorded == []


def test_skips_tasks_with_no_pr_terminal_or_paused() -> None:
    calls: list[list[str]] = []
    client = _FakeClient()
    w = ReviewWatcher(client, run=_runner(_OPEN_REVIEW_REQUIRED, calls=calls))  # type: ignore[arg-type]
    assert w.observe(_task(url=None)) is None
    assert w.observe(_task(state="COMPLETE")) is None
    assert w.observe(_task(paused=True)) is None
    assert calls == []  # no forge call for any of them
    assert client.recorded == []


def test_unchanged_value_is_not_re_recorded() -> None:
    # The watcher runs every pass; re-posting the same value would churn the change feed the
    # dashboard long-polls.
    client = _FakeClient()
    w = ReviewWatcher(client, run=_runner(_OPEN_REVIEW_REQUIRED))  # type: ignore[arg-type]
    assert w.observe(_task(waiting_on="external-review")) is WaitingOn.EXTERNAL_REVIEW
    assert client.recorded == []


def test_throttle_limits_forge_reads() -> None:
    # The host wakes on the change feed, not a timer, so an unthrottled watcher would be one `gh`
    # call per task per tick.
    calls: list[list[str]] = []
    clock = [1000.0]
    client = _FakeClient()
    w = ReviewWatcher(
        client,
        run=_runner(_OPEN_REVIEW_REQUIRED, calls=calls),  # type: ignore[arg-type]
        now=lambda: clock[0],
        poll_interval=60.0,
    )
    w.observe(_task())
    w.observe(_task(waiting_on="external-review"))  # same tick — throttled
    assert len(calls) == 1
    clock[0] += 61.0
    w.observe(_task(waiting_on="external-review"))  # past the interval — reads again
    assert len(calls) == 2


def test_emitted_command_is_a_json_pr_read() -> None:
    calls: list[list[str]] = []
    w = ReviewWatcher(_FakeClient(), run=_runner(_OPEN_REVIEW_REQUIRED, calls=calls))  # type: ignore[arg-type]
    w.observe(_task())
    assert calls[0][:4] == ["gh", "pr", "view", "https://github.com/acme/widgets/pull/7"]
    assert "state,reviewDecision,statusCheckRollup" in calls[0]
