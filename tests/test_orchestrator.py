"""The Orchestrator workflow: a valid, agent-driven, single-state lifecycle that opts in to
orchestrating other tasks and carries the `spawn-task` skill."""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition
from panopticon.workflows import Orchestrator

WF = Orchestrator()


def test_starts_orchestrating_with_user_turn() -> None:
    task = WF.start_task("t1", "r1", at="2026-06-23T00:00:00Z")
    assert task.state == "ORCHESTRATING"
    assert task.turn is Actor.USER  # initial state → the agent waits for the user's request first
    assert task.workflow == "orchestrator"
    assert [h.to_state for h in task.history] == ["ORCHESTRATING"]


def test_opts_in_to_orchestration() -> None:
    assert WF.orchestrates is True  # the capability gate the create/list MCP tools check


def test_orchestrating_reaches_complete_and_dropped() -> None:
    assert set(WF.transitions("ORCHESTRATING")) == {"COMPLETE", "DROPPED"}  # DROPPED inherited


def test_advance_derives_to_complete() -> None:
    assert WF.operations("ORCHESTRATING").get("advance") == "COMPLETE"


def test_ungated_transition_to_complete() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "COMPLETE", at="t1", trigger="advance")
    assert task.state == "COMPLETE"
    assert [h.to_state for h in task.history] == ["ORCHESTRATING", "COMPLETE"]


def test_can_always_drop() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "DROPPED", at="t1")
    assert task.state == "DROPPED"


def test_exposes_spawn_task_and_review_task_skills() -> None:
    names = {s.name for s in WF.skills()}
    assert names == {"spawn-task", "review-task"}
    assert all(s.description and s.instructions for s in WF.skills())


def test_carries_no_forge_plumbing() -> None:
    assert WF.image_layer() == ""  # works purely through MCP — no `gh`, no image layer
    assert tuple(WF.tools()) == ()


def test_cannot_leave_terminal() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "COMPLETE", at="t1")
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "DROPPED", at="t2")
