"""The agent-facing briefing — `Workflow.briefing` / `Workflow.overview`: tell the agent which
phase it's in (and let a workflow inject/override pieces).

Rendered from the workflow + task (LLM-free), emitted by the container's user-prompt hook so the
agent doesn't charge ahead — e.g. start implementing during a github-peer-reviewed task's PLANNING phase.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.artifacts import ArtifactStore
from panopticon.core.models import Task
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.workflows import GithubPeerReviewed, Spike

#: Golden fixtures: the exact prompts the github-peer-reviewed workflow generates. Regenerate after an
#: intentional wording change with ``UPDATE_FIXTURES=1 uv run pytest tests/test_briefing.py`` and commit the diff.
FIXTURES = Path(__file__).parent / "fixtures" / "briefing"


def _artifacts(tmp_path: Path) -> ArtifactStore:
    return FilesystemArtifactStore(tmp_path)


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


def test_ordered_phases_walks_the_advance_edges() -> None:
    # The happy path as a line: from the initial state, follow `advance` to the terminal state.
    assert GithubPeerReviewed().ordered_phases() == ["PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE"]


def test_briefing_names_the_phase_responsibilities_and_user_advance(tmp_path: Path) -> None:
    wf = GithubPeerReviewed()
    task = wf.start_task("t1", "r1", at="t0")  # initial state: PLANNING (user-advanced)

    text = wf.briefing(task, artifacts=_artifacts(tmp_path))

    assert "PLANNING" in text  # the agent learns which phase it's in
    assert "later phase" in text  # ... and not to do later-phase work (e.g. implementing)
    assert "plan-written" in text and "plan artifact" in text  # this phase's responsibility
    assert "Produce a plan for the implementation." in text  # PLANNING's description (what it's for)
    assert "ITERATING" in text  # the advance target
    # PLANNING is user-advanced, so the agent should hand back, not advance itself
    assert "hand back to the user" in text and "Don't advance on your own" in text


def test_briefing_for_an_agent_advanced_phase(tmp_path: Path) -> None:
    wf = GithubPeerReviewed()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "MERGING", at="t1")

    text = wf.briefing(task, artifacts=_artifacts(tmp_path))

    assert "MERGING" in text and "pr-merged" in text
    assert "advance the task yourself" in text  # MERGING is agent-advanced (background)
    assert "hand back" not in text  # it auto-advances — the briefing must not tell it to hand back


def test_briefing_for_a_terminal_state(tmp_path: Path) -> None:
    wf = GithubPeerReviewed()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "COMPLETE", at="t1")

    text = wf.briefing(task, artifacts=_artifacts(tmp_path))

    assert "COMPLETE" in text and "finished" in text  # nothing to do in a terminal state


def test_workflow_overview_maps_the_ordered_phases() -> None:
    text = GithubPeerReviewed().overview()

    # the whole lifecycle, in advance order, ending at the terminal state
    order = [text.index(p) for p in ("PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE")]
    assert order == sorted(order)
    assert "plan-written" in text and "pr-merged" in text  # each phase's responsibilities
    # each phase carries its own description (what it's for), sourced from cloude-cade
    assert "Produce a plan for the implementation." in text  # PLANNING
    assert "Wait for review or approval of the PR." in text  # REVIEW
    assert "Add the PR to the merge queue." in text  # MERGING
    # the responsibility gate and who advances are two separate sentences (gate before the bullets,
    # advance after them) — meeting the responsibilities is not what triggers the advance
    assert "You must meet these responsibilities before ending your turn — mark each as met the moment you complete it:" in text
    assert "The user will advance to the next state." in text  # user-advanced phases
    assert "Automatically advance to the next state." in text  # MERGING (agent-advanced)
    assert "terminal" in text  # COMPLETE
    assert "`advance`" in text and "`drop`" in text and "free move" in text  # the mechanics
    # the Tools section names the workflow's expected tools (github-peer-reviewed ships `gh`)
    assert "## Tools" in text and "`gh`" in text and "GitHub CLI" in text


def test_workflow_overview_handles_a_phase_with_no_responsibilities() -> None:
    # spike's ITERATING declares no responsibilities — the line must not dangle a colon + empty list.
    text = Spike().overview()
    assert "ITERATING" in text
    assert "Open-ended agent work until the user marks the task complete." in text  # its description
    # with no responsibilities the gate sentence is omitted — just the advance sentence
    assert "The user will advance to the next state." in text
    assert "responsibilities before ending your turn" not in text  # no gate sentence with nothing listed
    assert "## Tools" not in text  # spike declares no tools → the section is omitted


# -- the extension seam: a workflow injects/overrides briefing pieces ----------------------


class _ExtrasWorkflow(Workflow):
    """A minimal workflow that injects extra lines into both the briefing and the overview — the
    seam a forge workflow uses (e.g. to surface its plan artifact's URI)."""

    name = "extras-test"

    class Planning(InitialState):
        label = "PLANNING"
        description = "Plan it."
        transitions = (Complete,)

    initial = Planning

    def _overview_extras(self) -> Sequence[str]:
        return ["Injected overview note."]

    def _briefing_extras(self, task: Task, *, artifacts: ArtifactStore) -> Sequence[str]:
        # The hook receives the artifact store, so it can key off what's been written for the task.
        names = artifacts.list(task.id)
        return [f"Artifacts so far: {', '.join(names) or 'none'}."]


def test_overview_extras_are_appended() -> None:
    text = _ExtrasWorkflow().overview()
    assert "Injected overview note." in text


def test_briefing_extras_are_appended_and_get_the_artifact_store(tmp_path: Path) -> None:
    wf = _ExtrasWorkflow()
    task = wf.start_task("t1", "r1", at="t0")
    artifacts = _artifacts(tmp_path)

    # No artifacts yet → the hook (given the store) renders accordingly.
    assert "Artifacts so far: none." in wf.briefing(task, artifacts=artifacts)

    # Write one → the hook sees it, proving the seam is handed the live artifact store.
    artifacts.put(task.id, "plan.md", b"# Plan")
    assert "Artifacts so far: plan.md." in wf.briefing(task, artifacts=artifacts)


def test_default_extras_leave_the_output_unchanged(tmp_path: Path) -> None:
    # A workflow that overrides neither hook gets exactly the generic rendering (no stray blank
    # lines / separators) — the seam is opt-in.
    wf, task = _gpr_task_in("PLANNING")
    assert not wf.briefing(task, artifacts=_artifacts(tmp_path)).endswith("\n")
    assert not wf.overview().endswith("\n")


# -- golden fixtures: the exact github-peer-reviewed prompts ------------------------------------------


def test_github_peer_reviewed_system_prompt_matches_fixture() -> None:
    # The whole-workflow system prompt (the map + tools), captured verbatim.
    _assert_matches_fixture("github_peer_reviewed_system_prompt.md", GithubPeerReviewed().overview())


@pytest.mark.parametrize("state", ["PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE"])
def test_github_peer_reviewed_state_briefing_matches_fixture(state: str, tmp_path: Path) -> None:
    # The per-turn briefing for each github-peer-reviewed phase, captured verbatim.
    wf, task = _gpr_task_in(state)
    _assert_matches_fixture(f"github_peer_reviewed_state_{state}.md", wf.briefing(task, artifacts=_artifacts(tmp_path)))
