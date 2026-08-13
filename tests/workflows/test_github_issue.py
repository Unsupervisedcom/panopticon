"""The GithubIssue workflow: `github-self-reviewed` specialised for fixing a linked GitHub issue.

The golden behavioral spec — the collapsed graph (`PLANNING → ITERATING → MERGING → COMPLETE`,
no REVIEW), the foreground/background (advanced_by) policy, the issue-specific PLANNING
comprehension responsibility (`issue-understood`, naming all five axes), the tailored skill set
(a `read-issue` skill on top of the inherited `open-pr`/`babysit-ci`/`babysit-merge`), the
inherited forge plumbing (`gh` tool + image layer), full-lifecycle gating, the iterate-back free
move, the inability to skip straight to merging, and the universal drop.
"""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import GithubIssue
from panopticon.workflows.github_forge import GithubForgeWorkflow

WF = GithubIssue()


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
    assert task.workflow == "github-issue"
    assert [h.to_state for h in task.history] == ["PLANNING"]


def test_transition_graph_is_the_happy_path_plus_drop() -> None:
    # No REVIEW state — ITERATING advances straight to MERGING (the user self-reviews the fix).
    # Backward edges (iterate) are free moves, not declared transitions.
    assert set(WF.transitions("PLANNING")) == {"ITERATING", "DROPPED"}
    assert set(WF.transitions("ITERATING")) == {"MERGING", "DROPPED"}
    assert set(WF.transitions("MERGING")) == {"COMPLETE", "DROPPED"}
    assert list(WF.transitions("COMPLETE")) == []
    assert "REVIEW" not in set(WF.labels())  # no peer-review state


def test_foreground_states_are_user_advanced_merging_is_agent_driven() -> None:
    assert WF.advanced_by("PLANNING") is Actor.USER
    assert WF.advanced_by("ITERATING") is Actor.USER  # the user self-reviews, then advances
    assert WF.advanced_by("MERGING") is Actor.AGENT  # background: agent shepherds the merge


# -- responsibilities ---------------------------------------------------------------


def test_planning_gates_the_issue_comprehension() -> None:
    # PLANNING carries the two shared promises (plan.md artifact + token estimate) and the
    # issue-specific `issue-understood`. `url-recorded` is NOT here — the PR is an ITERATING
    # output (unlike Dependabot, whose PR URL is a known input).
    by_key = {r.key: r for r in WF.responsibilities("PLANNING")}
    assert set(by_key) == {"plan-written", "token-estimated", "issue-understood"}
    assert "url-recorded" not in by_key  # the PR URL is produced in ITERATING, not PLANNING
    # shared conventions, single-sourced on the forge/planned base
    assert (
        "plan.md" in by_key["plan-written"].description
        and "markdown" in by_key["plan-written"].description
    )
    assert "set_token_estimate" in by_key["token-estimated"].description
    # the comprehension responsibility names all five axes
    understood = by_key["issue-understood"].description.lower()
    assert "problem" in understood  # (1) the reported problem
    assert "root cause" in understood  # (2) root cause
    assert "reproduc" in understood or "confirm" in understood  # (3) reproduction / confirmation
    assert "fix approach" in understood  # (4) the fix approach
    assert "tests" in understood and "regression" in understood  # (5) tests / regression guard


def test_iterating_responsibilities_match_self_reviewed() -> None:
    # A fresh PR is opened here, so `url-recorded` stays in ITERATING — the same 7 as
    # github-self-reviewed.
    assert {r.key for r in WF.responsibilities("ITERATING")} == {
        "plan-implemented",
        "requests-implemented",
        "tests-pass",
        "committed-pushed",
        "ci-passing",
        "pr-updated",
        "url-recorded",
    }
    by_key = {r.key: r for r in WF.responsibilities("ITERATING")}
    assert "fix" in by_key["plan-implemented"].description.lower()  # implement the *fix*
    assert by_key["url-recorded"].description == GithubForgeWorkflow.URL_RECORDED.description


def test_merging_responsibility() -> None:
    assert {r.key for r in WF.responsibilities("MERGING")} == {"pr-merged"}


# -- skills + forge plumbing --------------------------------------------------------


def test_skills_add_read_issue_on_top_of_the_forge_skills() -> None:
    skills = {s.name: s for s in WF.skills()}
    assert set(skills) == {"read-issue", "open-pr", "babysit-ci", "babysit-merge"}
    read = skills["read-issue"]
    assert read.description and read.instructions  # a functional spec, not a stub
    assert "gh issue view" in read.instructions  # reads the issue
    assert "memo" in read.instructions  # reads the issue URL from the task memo
    assert "Closes" in read.instructions  # notes the number so the PR closes the issue


def test_forge_skills_are_reused_verbatim_from_the_base() -> None:
    base = {s.name: s for s in GithubForgeWorkflow().skills()}
    ours = {s.name: s for s in WF.skills()}
    for name in ("open-pr", "babysit-ci", "babysit-merge"):
        assert ours[name].instructions == base[name].instructions  # not re-authored


def test_inherits_the_gh_tool_and_image_layer() -> None:
    assert "gh" in {t.name for t in WF.tools()}  # named in the agent's system prompt
    assert "gh" in WF.image_layer()  # forge skills need gh layered onto the base image


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
    assert [h.to_state for h in task.history] == ["PLANNING", "ITERATING", "MERGING", "COMPLETE"]
    assert WF.is_terminal("COMPLETE")


# -- gating -------------------------------------------------------------------------


def test_cannot_advance_with_unresolved_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # comprehension promises still PENDING


def test_partial_resolution_still_gates() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    task.resolve_responsibility(key="plan-written", status=Status.MET)
    task.resolve_responsibility(key="token-estimated", status=Status.MET)
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # issue-understood still PENDING


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
