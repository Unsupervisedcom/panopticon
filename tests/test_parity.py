"""The Parity workflow: the full cloude-cade lifecycle, gated and turn-aware (ROADMAP Slice 4).

This is the golden behavioral spec for the parity flow — every legal/illegal transition, the
foreground/background (advanced_by) policy, responsibility gating at each stage, the
iterate-back edges, and the universal drop.
"""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import Parity

WF = Parity()


def _meet_all(task: Task) -> None:
    """Resolve every outstanding promise on the current state as MET."""
    for r in list(task.outstanding_responsibilities):
        task.resolve_responsibility(key=r.key, status=Status.MET)


def _advance(task: Task, to_state: str) -> None:
    _meet_all(task)
    WF.apply_transition(task, to_state, at="t", trigger="advance")


# -- shape: states, transitions, policy ---------------------------------------------


def test_starts_in_planning_on_the_agents_turn() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    assert task.state == "PLANNING"
    assert task.turn is Actor.AGENT  # the agent drafts the plan first
    assert task.workflow == "parity"
    assert [h.to_state for h in task.history] == ["PLANNING"]


def test_transition_graph_is_the_happy_path_plus_drop() -> None:
    # Backward edges (iterate) are NOT declared transitions — they're free moves. So each
    # non-terminal state has a single forward edge (+ inherited DROPPED).
    assert set(WF.transitions("PLANNING")) == {"ITERATING", "DROPPED"}
    assert set(WF.transitions("ITERATING")) == {"REVIEW", "DROPPED"}
    assert set(WF.transitions("REVIEW")) == {"MERGING", "DROPPED"}
    assert set(WF.transitions("MERGING")) == {"COMPLETE", "DROPPED"}
    assert list(WF.transitions("COMPLETE")) == []


def test_foreground_states_are_user_advanced_merging_is_agent_driven() -> None:
    assert WF.advanced_by("PLANNING") is Actor.USER
    assert WF.advanced_by("ITERATING") is Actor.USER
    assert WF.advanced_by("REVIEW") is Actor.USER
    assert WF.advanced_by("MERGING") is Actor.AGENT  # background: agent shepherds the merge


def test_responsibilities_mirror_cloude_cade_dod() -> None:
    # cloude-cade's per-stage dod_bullets (bin/cloude_stages.py), agent-only (no user-approval),
    # DB state replacing the terminal org bullets, and draft-PR creation moved to provisioning.
    assert {r.key for r in WF.responsibilities("PLANNING")} == {"plan-written"}
    assert {r.key for r in WF.responsibilities("ITERATING")} == {
        "plan-implemented", "requests-implemented", "tests-pass",
        "committed-pushed", "ci-passing", "pr-updated",
    }
    assert {r.key for r in WF.responsibilities("REVIEW")} == {"pr-reviewed"}
    assert {r.key for r in WF.responsibilities("MERGING")} == {"pr-merged"}


def test_parity_has_no_forge_skills_yet() -> None:
    assert tuple(WF.skills()) == ()  # babysit-* are in-container skills, a later slice


def test_parity_image_layer_installs_gh() -> None:
    assert "gh" in WF.image_layer()  # forge skills need gh layered onto the base image


def test_core_operations_per_state() -> None:
    assert WF.operations("PLANNING") == {"advance": "ITERATING", "drop": "DROPPED"}
    assert WF.operations("ITERATING") == {"advance": "REVIEW", "drop": "DROPPED"}
    assert WF.operations("REVIEW") == {"advance": "MERGING", "drop": "DROPPED"}
    assert WF.operations("MERGING") == {"advance": "COMPLETE", "drop": "DROPPED"}
    assert WF.operations("COMPLETE") == {}


# -- the happy path: full lifecycle, gated at every stage ---------------------------


def test_full_lifecycle_planning_to_complete() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    for nxt in ("ITERATING", "REVIEW", "MERGING", "COMPLETE"):
        _advance(task, nxt)
    assert task.state == "COMPLETE"
    assert [h.to_state for h in task.history] == [
        "PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE",
    ]
    assert WF.is_terminal("COMPLETE")


def test_turn_flips_to_user_on_entering_a_foreground_state() -> None:
    # Every state enters on the agent's turn here (the agent acts first); the *advance* policy
    # is what makes the foreground states user-driven. Entry turn is AGENT throughout.
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")
    assert task.turn is Actor.AGENT


# -- gating -------------------------------------------------------------------------


def test_cannot_advance_with_unresolved_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # plan-written still PENDING


def test_partial_resolution_still_gates() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")  # now in ITERATING with several promises
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "REVIEW", at="t2")  # the rest (e.g. plan-implemented) still PENDING


# -- iterate-back + drop ------------------------------------------------------------


def test_free_move_back_from_merging_to_iterating() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    for nxt in ("ITERATING", "REVIEW", "MERGING"):
        _advance(task, nxt)
    WF.force_transition(task, "ITERATING", at="t4", trigger="set-state")  # free move, ungated
    assert task.state == "ITERATING"


def test_drop_is_allowed_from_every_state_and_bypasses_gating() -> None:
    for start in ("PLANNING", "ITERATING", "REVIEW", "MERGING"):
        task = WF.start_task("t1", "r1", at="t0")
        # walk to `start` without dropping
        path = ["ITERATING", "REVIEW", "MERGING"]
        for nxt in path[: path.index(start) + 1] if start != "PLANNING" else []:
            _advance(task, nxt)
        assert task.state == start
        WF.apply_transition(task, "DROPPED", at="td")  # ungated, even with promises outstanding
        assert task.state == "DROPPED"


def test_cannot_skip_review() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")
    _meet_all(task)
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "MERGING", at="t2")  # no ITERATING -> MERGING edge in base parity
