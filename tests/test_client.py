"""The shared task-service REST client (``panopticon.client.TaskServiceClient``) against the
real FastAPI app via ``TestClient`` — the surface both the container harness and the terminal
controller depend on. (The end-to-end liveness/registration path is in test_skeleton.py.)"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TaskServiceClient]:
    service = TaskService(SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


def test_reads_workflows_repos_and_tasks(client: TaskServiceClient) -> None:
    assert client.list_workflows() == ["spike"]
    assert [r["id"] for r in client.list_repos()] == ["r1"]
    task = client.create_task("r1", "spike")
    listed = client.list_tasks()
    assert [t["id"] for t in listed] == [task["id"]]
    assert client.get_task(task["id"])["state"] == "ITERATING"


def test_drives_slug_and_transition(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    client.set_slug(task_id, "fix-widget")
    assert client.list_transitions(task_id) == ["COMPLETE", "DROPPED"]
    done = client.request_transition(task_id, "COMPLETE", trigger="finish")
    assert done["state"] == "COMPLETE"
    assert client.get_task(task_id)["slug"] == "fix-widget"


def test_drives_core_operations(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.list_operations(task_id) == {"advance": "COMPLETE", "drop": "DROPPED"}
    done = client.apply_operation(task_id, "advance")
    assert done["state"] == "COMPLETE"


def test_create_repo_over_rest(client: TaskServiceClient) -> None:
    client.create_repo("r2", "acme/other", "https://x/r2.git")
    assert {r["id"] for r in client.list_repos()} == {"r1", "r2"}
