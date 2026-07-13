"""Unit tests for the shared setup-repo task helper."""

from __future__ import annotations

from typing import Any

from panopticon.terminal import setup_repo_task as srt


def test_workflow_name_matches_the_workflow() -> None:
    from panopticon.workflows.setup_repo import SetupRepo

    assert srt.SETUP_REPO_WORKFLOW == SetupRepo.name == "setup-repo"


def test_setup_repo_memo_names_the_repo() -> None:
    assert srt.setup_repo_memo("acme/widgets") == "Set up the acme/widgets repo."


def test_create_setup_repo_task_uses_workflow_and_memo() -> None:
    created: dict[str, Any] = {}

    class _Client:
        def create_task(
            self, repo_id: str, workflow: str, memo: str | None = None, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, workflow=workflow, memo=memo)
            return {"id": "t1"}

    result = srt.create_setup_repo_task(_Client(), "r1", "acme/widgets")  # type: ignore[arg-type]
    assert result == {"id": "t1"}
    assert created == {
        "repo_id": "r1",
        "workflow": "setup-repo",
        "memo": "Set up the acme/widgets repo.",
    }
