"""Golden behavioral spec for the deterministic resident-agent workflow."""

from __future__ import annotations

import pytest

from panopticon.core import Actor, ResponsibilitiesNotMet
from panopticon.core.models import Status, Task
from panopticon.workflows import ResidentAgent
from panopticon.workflows.resident_agent import issue_title_and_body

WF = ResidentAgent()


def _meet_all(task: Task) -> None:
    for responsibility in list(task.outstanding_responsibilities):
        task.resolve_responsibility(key=responsibility.key, status=Status.MET)


def _advance(task: Task, state: str) -> None:
    _meet_all(task)
    WF.apply_transition(task, state, at="t", trigger="watcher")


def test_shape_policy_and_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    assert (task.state, task.turn, task.workflow) == ("FILING", Actor.USER, "resident-agent")
    assert WF.runner_type == "forge"
    assert WF.skills() == ()
    assert all(
        WF.advanced_by(state) is Actor.AGENT
        for state in ("FILING", "IMPLEMENTING", "REVIEW", "MERGING")
    )
    assert {r.key for r in WF.responsibilities("FILING")} == {"issue-filed"}
    assert {r.key for r in WF.responsibilities("IMPLEMENTING")} == {"pr-opened"}
    assert {r.key for r in WF.responsibilities("REVIEW")} == {
        "pr-ready",
        "review-requested",
        "pr-approved",
    }
    assert {r.key for r in WF.responsibilities("MERGING")} == {"pr-merged"}


def test_happy_path_is_gated() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "IMPLEMENTING", at="t1")
    for state in ("IMPLEMENTING", "REVIEW", "MERGING", "COMPLETE"):
        _advance(task, state)
    assert task.state == "COMPLETE"


def test_review_can_move_freely_back_to_implementing() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    _advance(task, "IMPLEMENTING")
    _advance(task, "REVIEW")
    WF.force_transition(task, "IMPLEMENTING", at="t3", trigger="changes-requested")
    assert task.state == "IMPLEMENTING"


def test_drop_is_available_from_every_state() -> None:
    path = ("FILING", "IMPLEMENTING", "REVIEW", "MERGING")
    for index, state in enumerate(path):
        task = WF.start_task("t1", "r1", at="t0")
        for destination in path[1 : index + 1]:
            _advance(task, destination)
        assert task.state == state
        WF.apply_transition(task, "DROPPED", at="td")
        assert task.state == "DROPPED"


@pytest.mark.parametrize(
    ("task", "artifact", "title", "body_start"),
    [
        ({"id": "t1", "memo": "Fix widgets\nDetails here"}, None, "Fix widgets", "Details here"),
        (
            {"id": "t2", "memo": "Fix widgets\nMemo body", "initial_prompt": "Prompt body"},
            "Artifact body",
            "Fix widgets",
            "Artifact body",
        ),
    ],
)
def test_issue_title_and_body_sources(
    task: dict[str, str], artifact: str | None, title: str, body_start: str
) -> None:
    actual_title, body = issue_title_and_body(task, artifact)
    assert actual_title == title
    assert body.startswith(body_start)
    assert body.splitlines()[-1] == f"Panopticon task: {task['id']}"


def test_initial_prompt_wins_over_memo_body() -> None:
    _, body = issue_title_and_body(
        {"id": "t1", "memo": "Title\nMemo body", "initial_prompt": "Prompt body"}
    )
    assert body.startswith("Prompt body")


@pytest.mark.asyncio
async def test_briefing_explains_no_agent_and_url_convention() -> None:
    class _Artifacts:
        async def list(self, task_id: str) -> list[str]:
            return []

    task = WF.start_task("t1", "r1", at="t0")
    briefing = await WF.briefing(task, artifacts=_Artifacts())  # type: ignore[arg-type]
    assert "No Panopticon agent runs" in briefing
    assert "issue until a pull request exists" in briefing
