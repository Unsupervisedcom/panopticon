"""The GithubDependabot workflow: `github-self-reviewed` specialised for a Dependabot bump PR.

The golden behavioral spec — the collapsed graph (`PLANNING → ITERATING → MERGING → COMPLETE`,
no REVIEW), the foreground/background (advanced_by) policy, the dependabot-specific PLANNING
evaluation responsibility (`upgrade-evaluated`, naming all five axes), the tailored skill set
(`checkout-dependabot-pr` in place of `open-pr`, reusing the inherited `babysit-ci`/
`babysit-merge`), the inherited forge plumbing (`gh` tool + image layer), full-lifecycle gating,
the iterate-back free move, the inability to skip straight to merging, and the universal drop.
"""

from __future__ import annotations

import pytest

from panopticon.core import Actor, IllegalTransition, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import GithubDependabot
from panopticon.workflows.github_forge import GithubForgeWorkflow

WF = GithubDependabot()


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
    assert task.workflow == "github-dependabot"
    assert [h.to_state for h in task.history] == ["PLANNING"]


def test_transition_graph_is_the_happy_path_plus_drop() -> None:
    # No REVIEW state — ITERATING advances straight to MERGING (the user self-reviews the
    # evaluation). Backward edges (iterate) are free moves, not declared transitions.
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


def test_planning_gates_the_dependabot_evaluation() -> None:
    # PLANNING carries the shared plan.md promise, the dependabot-specific `upgrade-evaluated`,
    # and `url-recorded` — the PR URL is a known input (the task memo), so it is recorded and
    # gated here, not in ITERATING.
    by_key = {r.key: r for r in WF.responsibilities("PLANNING")}
    assert set(by_key) == {"plan-written", "upgrade-evaluated", "url-recorded"}
    # shared conventions, single-sourced on the forge/planned base
    assert (
        "plan.md" in by_key["plan-written"].description
        and "markdown" in by_key["plan-written"].description
    )
    # the URL responsibility is the shared forge one, now gated in PLANNING
    assert by_key["url-recorded"].description == GithubForgeWorkflow.URL_RECORDED.description
    # the evaluation responsibility names all five axes
    evaluation = by_key["upgrade-evaluated"].description.lower()
    assert "used" in evaluation  # (1) how the module is used
    assert "relevant" in evaluation  # (2) relevance
    assert "risk" in evaluation and "semver" in evaluation  # (3) risk incl. semver
    assert "security" in evaluation  # (4) security / urgency
    assert "recommend" in evaluation and "none" in evaluation  # (5) recommended changes, none ok


def test_iterating_responsibilities_target_the_dependabot_pr() -> None:
    by_key = {r.key: r for r in WF.responsibilities("ITERATING")}
    # `url-recorded` is gated in PLANNING (the URL is an input — the memo), not here.
    assert set(by_key) == {
        "plan-implemented",
        "requests-implemented",
        "tests-pass",
        "committed-pushed",
        "ci-passing",
    }
    # supporting changes are optional (the plan may recommend none)
    assert "none" in by_key["plan-implemented"].description.lower()
    # changes land on the Dependabot PR branch
    assert "Dependabot" in by_key["committed-pushed"].description


def test_merging_gates_approval_and_merge() -> None:
    # MERGING carries the shared `pr-merged` plus the dependabot-specific `pr-approved` gate — the
    # approval is an explicit, gated promise, so the agent cannot advance to COMPLETE without it.
    by_key = {r.key: r for r in WF.responsibilities("MERGING")}
    assert set(by_key) == {"pr-approved", "pr-merged"}
    approval = by_key["pr-approved"].description.lower()
    assert "approve" in approval and "branch protection" in approval


# -- skills + forge plumbing --------------------------------------------------------


def test_skills_swap_open_pr_for_checkout_dependabot_pr() -> None:
    skills = {s.name: s for s in WF.skills()}
    assert set(skills) == {
        "checkout-dependabot-pr",
        "approve-dependabot-pr",
        "babysit-ci",
        "babysit-merge",
    }
    assert "open-pr" not in skills  # nothing to open — the PR already exists
    checkout = skills["checkout-dependabot-pr"]
    assert checkout.description and checkout.instructions  # a functional spec, not a stub
    assert "gh pr checkout" in checkout.instructions
    assert "memo" in checkout.instructions  # reads the PR URL from the task memo
    assert "set_url" in checkout.instructions  # records the URL


def test_babysit_skills_are_reused_verbatim_from_the_forge_base() -> None:
    # The merge machinery stays shared: approval is a separate `approve-dependabot-pr` skill +
    # gated `pr-approved` responsibility, so `babysit-merge` itself is unchanged (no fork/drift).
    base = {s.name: s for s in GithubForgeWorkflow().skills()}
    ours = {s.name: s for s in WF.skills()}
    for name in ("babysit-ci", "babysit-merge"):
        assert ours[name].instructions == base[name].instructions  # not re-authored


def test_approve_skill_approves_the_pr_and_resolves_the_gate() -> None:
    # Approval is a dependabot-only skill that runs at the start of MERGING, before `babysit-merge`.
    skills = {s.name: s for s in WF.skills()}
    approve = skills["approve-dependabot-pr"]
    assert approve.description and approve.instructions  # a functional spec, not a stub
    assert "gh pr review" in approve.instructions and "--approve" in approve.instructions
    assert "pr-approved" in approve.instructions  # resolves the MERGING gate
    assert "babysit-merge" in approve.instructions  # then hands off to the merge shepherd


def test_forge_base_does_not_approve_prs() -> None:
    # Approval is scoped to dependabot: the shared base (and thus the self/peer-reviewed
    # lifecycles, where the agent is effectively the PR author) must NOT approve any PR.
    base_instructions = "\n".join(s.instructions for s in GithubForgeWorkflow().skills())
    assert "gh pr review" not in base_instructions
    assert "--approve" not in base_instructions


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
        WF.apply_transition(task, "ITERATING", at="t1")  # evaluation promises still PENDING


def test_partial_resolution_still_gates() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    task.resolve_responsibility(key="plan-written", status=Status.MET)
    task.resolve_responsibility(key="upgrade-evaluated", status=Status.MET)
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "ITERATING", at="t1")  # url-recorded (the PR input) still PENDING


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
