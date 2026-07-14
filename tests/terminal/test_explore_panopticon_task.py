"""Unit tests for the shared explore-panopticon task helper."""

from __future__ import annotations

from typing import Any

from panopticon.terminal import explore_panopticon_task as ept


def test_workflow_name_matches_the_workflow() -> None:
    from panopticon.workflows.explore_panopticon import ExplorePanopticon

    assert ept.EXPLORE_PANOPTICON_WORKFLOW == ExplorePanopticon.name == "explore-panopticon"


def test_create_explore_panopticon_task_uses_workflow_and_memo() -> None:
    created: dict[str, Any] = {}

    class _Client:
        def create_task(
            self, repo_id: str, workflow: str, memo: str | None = None, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, workflow=workflow, memo=memo)
            return {"id": "t1"}

    result = ept.create_explore_panopticon_task(_Client(), "r1")  # type: ignore[arg-type]
    assert result == {"id": "t1"}
    assert created == {
        "repo_id": "r1",
        "workflow": "explore-panopticon",
        "memo": ept.EXPLORE_PANOPTICON_MEMO,
    }
