"""The LocalGitSelfReviewed workflow: forge-free, local-commit lifecycle.

The golden behavioral spec for the local-git flow — the graph
(`PLANNING → ITERATING → MERGING → COMPLETE`, no REVIEW), foreground/background
advanced_by policy, responsibility gating at each stage, the iterate-back free move,
the inability to skip straight to COMPLETE, no forge dependencies, and the universal drop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.artifacts import mcp_uri
from panopticon.core.models import Status, Task
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.workflows import LocalGitSelfReviewed
from panopticon.workflows.planned_workflow import PlannedWorkflow

WF = LocalGitSelfReviewed()


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
    assert task.workflow == "local-git-self-reviewed"
    assert [h.to_state for h in task.history] == ["PLANNING"]


def test_transition_graph_is_the_happy_path_plus_drop() -> None:
    assert set(WF.transitions("PLANNING")) == {"ITERATING", "DROPPED"}
    assert set(WF.transitions("ITERATING")) == {"MERGING", "DROPPED"}
    assert set(WF.transitions("MERGING")) == {"COMPLETE", "DROPPED"}
    assert list(WF.transitions("COMPLETE")) == []
    assert "REVIEW" not in set(WF.labels())
    assert "MERGING" in set(WF.labels())


def test_foreground_states_are_user_advanced_merging_is_agent_driven() -> None:
    assert WF.advanced_by("PLANNING") is Actor.USER
    assert WF.advanced_by("ITERATING") is Actor.USER  # user self-reviews, then advances
    assert WF.advanced_by("MERGING") is Actor.AGENT  # background: agent drives the local merge


def test_responsibilities_are_local_git_specific() -> None:
    # PLANNING: same plan convention as the forge flows.
    assert {r.key for r in WF.responsibilities("PLANNING")} == {"plan-written", "token-estimated"}
    by_key = {r.key: r for r in WF.responsibilities("PLANNING")}
    assert (
        "plan.md" in by_key["plan-written"].description
        and "markdown" in by_key["plan-written"].description
    )
    assert "set_token_estimate" in by_key["token-estimated"].description

    # ITERATING: no forge obligations (no committed-pushed, no ci-passing, no pr-updated).
    assert {r.key for r in WF.responsibilities("ITERATING")} == {
        "plan-implemented",
        "requests-implemented",
        "tests-pass",
        "committed",
    }
    iterating_keys = {r.key for r in WF.responsibilities("ITERATING")}
    assert "committed-pushed" not in iterating_keys
    assert "ci-passing" not in iterating_keys
    assert "pr-updated" not in iterating_keys

    # MERGING: local branch merge, not a remote PR.
    assert {r.key for r in WF.responsibilities("MERGING")} == {"local-merged"}


def test_no_forge_tools() -> None:
    assert WF.tools() == ()  # no gh or other forge CLIs


def test_no_image_layer() -> None:
    assert WF.image_layer() == ""  # nothing extra to install


def test_has_local_merge_skill_but_no_forge_skills() -> None:
    skills = WF.skills()
    assert {s.name for s in skills} == {"local-merge"}
    assert all(s.description and s.instructions for s in skills)
    assert not any(s.name in {"open-pr", "babysit-ci", "babysit-merge"} for s in skills)


def test_plan_artifact_name_and_uri_inherited_from_planned_workflow() -> None:
    assert PlannedWorkflow.PLAN_ARTIFACT_NAME == "plan.md"
    assert LocalGitSelfReviewed.PLAN_ARTIFACT_NAME == "plan.md"  # inherited
    assert LocalGitSelfReviewed.plan_uri("t1") == mcp_uri("t1", "plan.md")
    assert LocalGitSelfReviewed.plan_uri("t1") == "panopticon://tasks/t1/artifacts/plan.md"


def test_briefing_surfaces_the_plan_uri_once_the_plan_artifact_exists(tmp_path: Path) -> None:
    artifacts = FilesystemArtifactStore(tmp_path)
    task = WF.start_task("t1", "r1", at="t0")

    assert "panopticon://" not in asyncio.run(
        WF.briefing(task, artifacts=artifacts)
    )  # no plan yet → no URI

    asyncio.run(artifacts.put(task.id, "plan.md", b"# Plan"))
    text = asyncio.run(WF.briefing(task, artifacts=artifacts))
    assert "panopticon://tasks/t1/artifacts/plan.md" in text
    assert "don't guess" in text


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
        "PLANNING",
        "ITERATING",
        "MERGING",
        "COMPLETE",
    ]
    assert WF.is_terminal("COMPLETE")


# -- gating -------------------------------------------------------------------------


def test_cannot_advance_from_planning_with_unresolved_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # plan-written/token-estimated PENDING


def test_cannot_advance_from_iterating_with_unresolved_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "MERGING", at="t2")  # committed etc. still PENDING


def test_partial_resolution_still_gates() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "MERGING", at="t2")  # committed + others still PENDING


# -- iterate-back + drop ------------------------------------------------------------


def test_free_move_back_from_iterating_to_planning() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "ITERATING")
    WF.force_transition(task, "PLANNING", at="t2", trigger="set-state")  # free move, ungated
    assert task.state == "PLANNING"


def test_free_move_back_from_merging_to_iterating() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    for nxt in ("ITERATING", "MERGING"):
        _advance(task, nxt)
    WF.force_transition(task, "ITERATING", at="t3", trigger="set-state")  # free move, ungated
    assert task.state == "ITERATING"


def test_drop_is_allowed_from_every_state_and_bypasses_gating() -> None:
    for start in ("PLANNING", "ITERATING", "MERGING"):
        task = WF.start_task("t1", "r1", at="t0")
        path = ["ITERATING", "MERGING"]
        for nxt in path[: path.index(start) + 1] if start != "PLANNING" else []:
            _advance(task, nxt)
        assert task.state == start
        WF.apply_transition(task, "DROPPED", at="td")  # ungated, even with promises outstanding
        assert task.state == "DROPPED"


def test_cannot_skip_straight_to_complete() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _meet_all(task)
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "COMPLETE", at="t1")  # no PLANNING -> COMPLETE edge
