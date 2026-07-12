"""The Spike seed workflow is a valid, ungated, agent-driven lifecycle."""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition
from panopticon.workflows import Spike

WF = Spike()


def test_starts_iterating_with_user_turn() -> None:
    task = WF.start_task("t1", "r1", at="2026-06-12T00:00:00Z")
    assert task.state == "ITERATING"
    assert task.turn is Actor.USER  # initial state → the agent waits for the user's first instruction
    assert task.workflow == "spike"
    assert [h.to_state for h in task.history] == ["ITERATING"]


def test_iterating_reaches_complete_and_dropped() -> None:
    assert set(WF.transitions("ITERATING")) == {"COMPLETE", "DROPPED"}  # DROPPED inherited


def test_complete_is_terminal() -> None:
    assert WF.is_terminal("COMPLETE")
    assert list(WF.transitions("COMPLETE")) == []


def test_ungated_transition_to_complete() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "COMPLETE", at="t1", trigger="finish")
    assert task.state == "COMPLETE"
    assert [h.to_state for h in task.history] == ["ITERATING", "COMPLETE"]


def test_can_always_drop() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "DROPPED", at="t1")
    assert task.state == "DROPPED"


def test_has_no_skills() -> None:
    assert tuple(WF.skills()) == ()


def test_cannot_leave_terminal() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "COMPLETE", at="t1")
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "DROPPED", at="t2")


def test_image_layer_is_empty() -> None:
    assert WF.image_layer() == ""  # forge-less seed adds no image layer
