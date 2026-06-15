"""Terminal controller: the dashboard REST client (against the real app) and the CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.terminal import __main__ as cli
from panopticon.terminal.client import DashboardClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[DashboardClient]:
    service = TaskService(SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    with TestClient(create_app(service)) as http:
        yield DashboardClient(http)


def test_reads_workflows_repos_and_tasks(client: DashboardClient) -> None:
    assert client.list_workflows() == ["spike"]
    assert [r["id"] for r in client.list_repos()] == ["r1"]
    task = client.create_task("r1", "spike")
    listed = client.list_tasks()
    assert [t["id"] for t in listed] == [task["id"]]
    assert client.get_task(task["id"])["state"] == "ITERATING"


def test_drives_slug_and_transition(client: DashboardClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    client.set_slug(task_id, "fix-widget")
    assert client.list_transitions(task_id) == ["COMPLETE", "DROPPED"]
    done = client.request_transition(task_id, "COMPLETE", trigger="finish")
    assert done["state"] == "COMPLETE"
    assert client.get_task(task_id)["slug"] == "fix-widget"


def test_create_repo_over_rest(client: DashboardClient) -> None:
    client.create_repo("r2", "acme/other", "https://x/r2.git")
    assert {r["id"] for r in client.list_repos()} == {"r1", "r2"}


class _FakeClient:
    def list_tasks(self) -> list[dict[str, object]]:
        return [{"id": "t1", "state": "ITERATING", "turn": "agent", "slug": None}]


def test_cli_tasks_lists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["tasks"], client=_FakeClient())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out and "ITERATING" in out and "agent" in out
