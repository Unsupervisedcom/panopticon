"""The GithubSelfReviewed workflow: `github-peer-reviewed` minus the peer-review gate.

The golden behavioral spec for the self-review flow — the collapsed graph
(`PLANNING → ITERATING → MERGING → COMPLETE`, no REVIEW), the foreground/background
(advanced_by) policy, responsibility gating at each stage, the iterate-back free move, the
inability to skip straight to merging, the inherited forge plumbing, and the universal drop.
"""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import GithubSelfReviewed

WF = GithubSelfReviewed()


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
    assert task.workflow == "github-self-reviewed"
    assert [h.to_state for h in task.history] == ["PLANNING"]


def test_transition_graph_is_the_happy_path_plus_drop() -> None:
    # No REVIEW state — ITERATING advances straight to MERGING. Backward edges (iterate) are
    # free moves, not declared transitions, so each non-terminal state has one forward edge.
    assert set(WF.transitions("PLANNING")) == {"ITERATING", "DROPPED"}
    assert set(WF.transitions("ITERATING")) == {"MERGING", "DROPPED"}
    assert set(WF.transitions("MERGING")) == {"COMPLETE", "DROPPED"}
    assert list(WF.transitions("COMPLETE")) == []
    assert "REVIEW" not in set(WF.labels())  # the peer-review state is gone


def test_foreground_states_are_user_advanced_merging_is_agent_driven() -> None:
    assert WF.advanced_by("PLANNING") is Actor.USER
    assert WF.advanced_by("ITERATING") is Actor.USER  # the user self-reviews, then advances
    assert WF.advanced_by("MERGING") is Actor.AGENT  # background: agent shepherds the merge


def test_responsibilities_drop_the_peer_review_obligation() -> None:
    # Same per-stage DoD as github-peer-reviewed, minus the REVIEW state's `pr-reviewed`
    # (there is no peer — the user self-reviews, which is the advance, not a responsibility).
    assert {r.key for r in WF.responsibilities("PLANNING")} == {"plan-written"}
    assert {r.key for r in WF.responsibilities("ITERATING")} == {
        "plan-implemented", "requests-implemented", "tests-pass",
        "committed-pushed", "ci-passing", "pr-updated",
    }
    assert {r.key for r in WF.responsibilities("MERGING")} == {"pr-merged"}


def test_github_self_reviewed_inherits_the_forge_skills() -> None:
    skills = WF.skills()
    assert {s.name for s in skills} == {"open-pr", "babysit-ci", "babysit-merge"}
    assert all(s.description and s.instructions for s in skills)  # functional specs, not stubs


def test_github_self_reviewed_image_layer_installs_gh() -> None:
    assert "gh" in WF.image_layer()  # forge skills need gh layered onto the base image


def test_github_self_reviewed_declares_gh_as_a_tool() -> None:
    names = {t.name for t in WF.tools()}  # named in the agent's system prompt (it ships in the image)
    assert "gh" in names


def test_core_operations_per_state() -> None:
    assert WF.operations("PLANNING") == {"advance": "ITERATING", "drop": "DROPPED"}
    assert WF.operations("ITERATING") == {"advance": "MERGING", "drop": "DROPPED"}
    assert WF.operations("MERGING") == {"advance": "COMPLETE", "drop": "DROPPED"}
    assert WF.operations("COMPLETE") == {}


# -- the happy path: full lifecycle, gated at every stage ---------------------------


def test_full_lifecycle_planning_to_complete() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    for nxt in ("ITERATING", "MERGING", "COMPLETE"):
        _advance(task, nxt)
    assert task.state == "COMPLETE"
    assert [h.to_state for h in task.history] == [
        "PLANNING", "ITERATING", "MERGING", "COMPLETE",
    ]
    assert WF.is_terminal("COMPLETE")


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
        WF.apply_transition(task, "MERGING", at="t2")  # the rest (e.g. plan-implemented) still PENDING


# -- iterate-back + drop ------------------------------------------------------------


def test_free_move_back_from_merging_to_iterating() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    for nxt in ("ITERATING", "MERGING"):
        _advance(task, nxt)
    WF.force_transition(task, "ITERATING", at="t3", trigger="set-state")  # free move, ungated
    assert task.state == "ITERATING"


def test_drop_is_allowed_from_every_state_and_bypasses_gating() -> None:
    for start in ("PLANNING", "ITERATING", "MERGING"):
        task = WF.start_task("t1", "r1", at="t0")
        # walk to `start` without dropping
        path = ["ITERATING", "MERGING"]
        for nxt in path[: path.index(start) + 1] if start != "PLANNING" else []:
            _advance(task, nxt)
        assert task.state == start
        WF.apply_transition(task, "DROPPED", at="td")  # ungated, even with promises outstanding
        assert task.state == "DROPPED"


def test_cannot_skip_straight_to_merging() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _meet_all(task)
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "MERGING", at="t1")  # no PLANNING -> MERGING edge
