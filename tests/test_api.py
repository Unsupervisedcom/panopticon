"""REST API contract tests via FastAPI's TestClient."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.core.models import Repo, Responsibility
from panopticon.core.state import Complete, State
from panopticon.core.workflow import Workflow
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.service import TaskService
from panopticon.workflows import Spike


class _GatedWorkflow(Workflow):
    """WORKING (agent) carries a responsibility gating the handoff to COMPLETE."""

    name = "gated"

    class Working(State):
        label = "WORKING"
        responsibilities = (Responsibility(key="tests-pass", description="Tests pass"),)
        transitions = (Complete,)

    initial = Working


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    with TestClient(create_app(service)) as c:
        yield c


def _new_task(client: TestClient) -> str:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_health_and_workflows(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/workflows").json() == ["spike"]


def test_create_and_get_task(client: TestClient) -> None:
    task_id = _new_task(client)
    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["state"] == "ITERATING"
    assert body["turn"] == "agent"
    assert body["slug"] is None
    assert [h["to_state"] for h in body["history"]] == ["ITERATING"]


def test_get_missing_task_404(client: TestClient) -> None:
    assert client.get("/tasks/ghost").status_code == 404


def test_create_task_unknown_workflow_400(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "nope"})
    assert resp.status_code == 400


def test_create_task_missing_repo_404(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "ghost", "workflow": "spike"})
    assert resp.status_code == 404


def test_legal_transition(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE", "trigger": "finish"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETE"


def test_illegal_transition_409(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(f"/tasks/{task_id}/transition", json={"to_state": "WORKING"})
    assert resp.status_code == 409


def test_set_slug(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.put(f"/tasks/{task_id}/slug", json={"slug": "fix-widget"})
    assert resp.status_code == 200
    assert resp.json()["slug"] == "fix-widget"


# -- artifacts ----------------------------------------------------------------------


def test_artifact_put_get_list(client: TestClient) -> None:
    task_id = _new_task(client)
    put = client.put(f"/tasks/{task_id}/artifacts/plan.md", content=b"# Plan")
    assert put.status_code == 204
    assert client.get(f"/tasks/{task_id}/artifacts/plan.md").content == b"# Plan"
    assert client.get(f"/tasks/{task_id}/artifacts").json() == ["plan.md"]


def test_artifact_missing_404(client: TestClient) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}/artifacts/plan.md").status_code == 404


# -- liveness -----------------------------------------------------------------------


def test_register_heartbeat_list_deregister(client: TestClient) -> None:
    task_id = _new_task(client)
    reg = client.post(
        f"/tasks/{task_id}/registrations", json={"container_id": "c-abc", "runner_id": "r-1"}
    )
    assert reg.status_code == 201
    reg_id = reg.json()["id"]

    assert client.post(f"/registrations/{reg_id}/heartbeat").status_code == 200

    listed = client.get(f"/tasks/{task_id}/registrations").json()
    assert [r["id"] for r in listed] == [reg_id]

    assert client.delete(f"/registrations/{reg_id}").status_code == 204
    assert client.get(f"/tasks/{task_id}/registrations").json() == []


# -- responsibilities over the wire -------------------------------------------------


@pytest.fixture
def gated_client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"gated": _GatedWorkflow()},
        FilesystemArtifactStore(tmp_path),
    )
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    with TestClient(create_app(service)) as c:
        yield c


def test_resolve_responsibility_then_transition(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]

    # WORKING has an unresolved promise → the transition is gated (409).
    bare = gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"})
    assert bare.status_code == 409

    # Report it met → 200, and the WORKING entry records the resolution in place.
    reported = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "tests-pass", "status": "met"}
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["history"][-1]["responsibilities"] == [
        {"key": "tests-pass", "description": "Tests pass", "status": "met", "comment": None}
    ]

    # Now the gate is clear → the transition succeeds.
    ok = gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"})
    assert ok.status_code == 200
    assert ok.json()["state"] == "COMPLETE"


def test_failed_responsibility_needs_comment(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    resp = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "tests-pass", "status": "failed"}
    )
    assert resp.status_code == 400  # FAILED without a comment is rejected at report time


def test_report_unknown_responsibility(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    resp = gated_client.post(
        f"/tasks/{task_id}/responsibilities", json={"key": "ghost", "status": "met"}
    )
    assert resp.status_code == 400
