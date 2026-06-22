"""The per-turn state briefing (`core.briefing`): tells the agent which phase it's in.

Rendered from the workflow + task (LLM-free), emitted by the container's user-prompt hook so the
agent doesn't charge ahead — e.g. start implementing during a github-peer-reviewed task's PLANNING phase.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from panopticon.core.briefing import render_state_briefing, render_workflow_overview
from panopticon.workflows import GithubPeerReviewed, Spike

#: Golden fixtures: the exact prompts the github-peer-reviewed workflow generates. Regenerate after an
#: intentional wording change with ``UPDATE_FIXTURES=1 uv run pytest tests/test_briefing.py`` and commit the diff.
FIXTURES = Path(__file__).parent / "fixtures" / "briefing"


def _assert_matches_fixture(name: str, actual: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_FIXTURES"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
    assert actual == path.read_text(), f"{name} drifted — regenerate with UPDATE_FIXTURES=1 and review"


def _gpr_task_in(state: str):  # type: ignore[no-untyped-def]
    wf = GithubPeerReviewed()
    task = wf.start_task("t1", "r1", at="t0")  # PLANNING
    if state != wf.initial_label:
        task = wf.force_transition(task, state, at="t1")
    return wf, task


def test_briefing_names_the_phase_responsibilities_and_user_advance() -> None:
    wf = GithubPeerReviewed()
    task = wf.start_task("t1", "r1", at="t0")  # initial state: PLANNING (user-advanced)

    text = render_state_briefing(wf, task)

    assert "PLANNING" in text  # the agent learns which phase it's in
    assert "later phase" in text  # ... and not to do later-phase work (e.g. implementing)
    assert "plan-written" in text and "plan artifact" in text  # this phase's responsibility
    assert "ITERATING" in text  # the advance target
    # PLANNING is user-advanced, so the agent should hand back, not advance itself
    assert "hand back to the user" in text and "Don't advance on your own" in text


def test_briefing_for_an_agent_advanced_phase() -> None:
    wf = GithubPeerReviewed()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "MERGING", at="t1")

    text = render_state_briefing(wf, task)

    assert "MERGING" in text and "pr-merged" in text
    assert "advance the task yourself" in text  # MERGING is agent-advanced (background)


def test_briefing_for_a_terminal_state() -> None:
    wf = GithubPeerReviewed()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "COMPLETE", at="t1")

    text = render_state_briefing(wf, task)

    assert "COMPLETE" in text and "finished" in text  # nothing to do in a terminal state


def test_workflow_overview_maps_the_ordered_phases() -> None:
    text = render_workflow_overview(GithubPeerReviewed())

    # the whole lifecycle, in advance order, ending at the terminal state
    order = [text.index(p) for p in ("PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE")]
    assert order == sorted(order)
    assert "plan-written" in text and "pr-merged" in text  # each phase's responsibilities
    assert "hand back to the user, who advances it" in text  # user-advanced phases
    assert "advance it yourself" in text  # MERGING (agent-advanced)
    assert "terminal" in text  # COMPLETE
    assert "`advance`" in text and "`drop`" in text and "free move" in text  # the mechanics
    # the Tools section names the workflow's expected tools (github-peer-reviewed ships `gh`)
    assert "## Tools" in text and "`gh`" in text and "GitHub CLI" in text


def test_workflow_overview_handles_a_phase_with_no_responsibilities() -> None:
    # spike's ITERATING declares no responsibilities — the line must not dangle a colon + empty list.
    text = render_workflow_overview(Spike())
    assert "ITERATING" in text
    assert "do the work, then hand back to the user, who advances it." in text
    assert "its responsibilities" not in text  # no "finish its responsibilities" with nothing under it
    assert "## Tools" not in text  # spike declares no tools → the section is omitted


# -- golden fixtures: the exact github-peer-reviewed prompts ------------------------------------------


def test_github_peer_reviewed_system_prompt_matches_fixture() -> None:
    # The whole-workflow system prompt (the map + tools), captured verbatim.
    _assert_matches_fixture("github_peer_reviewed_system_prompt.md", render_workflow_overview(GithubPeerReviewed()))


@pytest.mark.parametrize("state", ["PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE"])
def test_github_peer_reviewed_state_briefing_matches_fixture(state: str) -> None:
    # The per-turn briefing for each github-peer-reviewed phase, captured verbatim.
    wf, task = _gpr_task_in(state)
    _assert_matches_fixture(f"github_peer_reviewed_state_{state}.md", render_state_briefing(wf, task))
