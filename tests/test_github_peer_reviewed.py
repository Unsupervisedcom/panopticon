"""The GithubPeerReviewed workflow: the full cloude-cade lifecycle, gated and turn-aware (ROADMAP Slice 4).

This is the golden behavioral spec for the github-peer-reviewed flow — every legal/illegal transition, the
foreground/background (advanced_by) policy, responsibility gating at each stage, the
iterate-back edges, and the universal drop.
"""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import GithubPeerReviewed

WF = GithubPeerReviewed()


def _meet_all(task: Task) -> None:
    """Resolve every outstanding promise on the current state as MET."""
    for r in list(task.outstanding_responsibilities):
        task.resolve_responsibility(key=r.key, status=Status.MET)


def _advance(task: Task, to_state: str) -> None:
    _meet_all(task)
    WF.apply_transition(task, to_state, at="t", trigger="advance")


# -- shape: states, transitions, policy ---------------------------------------------


def test_starts_in_planning_on_the_users_turn() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    assert task.state == "PLANNING"
    assert task.turn is Actor.USER  # initial state → the agent waits for the user's first input
    assert task.workflow == "github-peer-reviewed"
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
    assert {r.key for r in WF.responsibilities("PLANNING")} == {"plan-written", "token-estimated"}
    # the plan is a markdown artifact (`plan.md`), so the dashboard opens it with the right handler
    by_key = {r.key: r for r in WF.responsibilities("PLANNING")}
    assert "plan.md" in by_key["plan-written"].description and "markdown" in by_key["plan-written"].description
    # planning also forecasts the task's cost via the set_token_estimate tool
    assert "set_token_estimate" in by_key["token-estimated"].description
    assert {r.key for r in WF.responsibilities("ITERATING")} == {
        "plan-implemented", "requests-implemented", "tests-pass",
        "committed-pushed", "ci-passing", "pr-updated",
    }
    assert {r.key for r in WF.responsibilities("REVIEW")} == {"pr-reviewed"}
    assert {r.key for r in WF.responsibilities("MERGING")} == {"pr-merged"}


def test_each_state_describes_its_phase() -> None:
    # every step carries a human-facing description (what the phase is for), sourced from
    # cloude-cade's per-stage prose — guards against the field regressing to empty.
    assert WF.description("PLANNING") == "Collect requirements. Produce a plan for the implementation."
    assert WF.description("ITERATING").startswith("Implement the plan.")
    assert WF.description("REVIEW") == "Wait for review or approval of the PR."
    assert WF.description("MERGING") == "Add the PR to the merge queue. If the PR exits the merge queue, re-add it."
    assert WF.description("COMPLETE")  # the terminal state is described too
    assert all(WF.description(label) for label in ("PLANNING", "ITERATING", "REVIEW", "MERGING"))


def test_github_peer_reviewed_exposes_forge_skills() -> None:
    skills = WF.skills()
    assert {s.name for s in skills} == {"open-pr", "babysit-ci", "babysit-merge"}
    assert all(s.description and s.instructions for s in skills)  # functional specs, not stubs


def test_babysit_merge_skill_covers_key_protocol_elements() -> None:
    merge_skill = next(s for s in WF.skills() if s.name == "babysit-merge")
    instructions = merge_skill.instructions
    assert "CLOSED" in instructions          # Gap 3: PR closed without merge
    assert "run_in_background" in instructions  # Gap 1: push-driven, non-blocking watch
    assert "state artifact" in instructions.lower() or "babysit-merge-state" in instructions  # Gap 2: cross-turn state (stored as task artifact)
    assert "double" in instructions.lower() or "already" in instructions.lower() or "autoMergeRequest" in instructions  # Gap 4: no double-queuing


def test_github_peer_reviewed_image_layer_installs_gh() -> None:
    assert "gh" in WF.image_layer()  # forge skills need gh layered onto the base image


def test_github_peer_reviewed_declares_gh_as_a_tool() -> None:
    names = {t.name for t in WF.tools()}  # named in the agent's system prompt (it ships in the image)
    assert "gh" in names


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
    # PLANNING (the initial state) enters on the user's turn; every *non-initial* state enters
    # on the agent's turn (the agent acts first). The *advance* policy is what makes the
    # foreground states user-driven, independent of the entry turn.
    task = WF.start_task("t1", "r1", at="t0")
    assert task.turn is Actor.USER  # PLANNING is the initial state
    _advance(task, "ITERATING")
    assert task.turn is Actor.AGENT  # a non-initial state enters on the agent's turn


# -- gating -------------------------------------------------------------------------


def test_cannot_advance_with_unresolved_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # plan-written/token-estimated still PENDING


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
        WF.apply_transition(task, "MERGING", at="t2")  # no ITERATING -> MERGING edge in the base workflow
