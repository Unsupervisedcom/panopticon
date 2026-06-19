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


def test_workflow_image_layer_endpoint(client: TestClient) -> None:
    # spike needs no layer (empty); the runner composes this onto the base image (ADR 0005).
    assert client.get("/workflows/spike/image-layer").json() == {"layer": ""}


def test_workflow_image_layer_surfaces_paritys_gh_layer(tmp_path: Path) -> None:
    from panopticon.workflows import Parity

    svc = TaskService(SqlAlchemyStore(), {"parity": Parity()}, FilesystemArtifactStore(tmp_path))
    with TestClient(create_app(svc)) as c:
        assert "gh" in c.get("/workflows/parity/image-layer").json()["layer"]  # forge skills need gh


def test_mcp_is_mounted(client: TestClient) -> None:
    # The MCP streamable-HTTP app is mounted at /mcp (in-container agents connect there); it must
    # be a mount, not a REST route, and reachable (i.e. not a REST 404).
    assert any(getattr(r, "path", None) == "/mcp" for r in client.app.routes)
    resp = client.get("/mcp/", headers={"Accept": "text/event-stream"})
    assert resp.status_code != 404


def test_create_and_get_task(client: TestClient) -> None:
    task_id = _new_task(client)
    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["state"] == "ITERATING"
    assert body["turn"] == "agent"
    assert body["slug"] is None
    assert body["description"] is None  # none given at creation
    assert body["provisioned"] is False  # no branch yet (computed Task.provisioned)
    assert [h["to_state"] for h in body["history"]] == ["ITERATING"]


def test_create_task_records_the_description(client: TestClient) -> None:
    resp = client.post(
        "/tasks", json={"repo_id": "r1", "workflow": "spike", "description": "make it green"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["description"] == "make it green"
    got = client.get(f"/tasks/{resp.json()['id']}")  # and it survives a reload
    assert got.json()["description"] == "make it green"


def test_get_missing_task_404(client: TestClient) -> None:
    assert client.get("/tasks/ghost").status_code == 404


def test_create_task_unknown_workflow_400(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "r1", "workflow": "nope"})
    assert resp.status_code == 400


def test_create_task_missing_repo_404(client: TestClient) -> None:
    resp = client.post("/tasks", json={"repo_id": "ghost", "workflow": "spike"})
    assert resp.status_code == 404


def test_list_legal_transitions(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.get(f"/tasks/{task_id}/transitions")
    assert resp.status_code == 200
    assert resp.json() == ["COMPLETE", "DROPPED"]  # spike ITERATING, sorted


def test_list_and_apply_operations(client: TestClient) -> None:
    task_id = _new_task(client)  # spike ITERATING
    ops = client.get(f"/tasks/{task_id}/operations")
    assert ops.json() == {"advance": "COMPLETE", "drop": "DROPPED"}  # advance derived, drop implicit

    advanced = client.post(f"/tasks/{task_id}/operations/advance")
    assert advanced.status_code == 200
    assert advanced.json()["state"] == "COMPLETE"
    assert advanced.json()["history"][-1]["trigger"] == "advance"  # the verb is the trigger


def test_apply_unavailable_operation_409(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.post(f"/tasks/{task_id}/operations/iterate")  # spike offers no iterate
    assert resp.status_code == 409


def test_list_states(client: TestClient) -> None:
    task_id = _new_task(client)
    assert set(client.get(f"/tasks/{task_id}/states").json()) == {"ITERATING", "COMPLETE", "DROPPED"}


def test_list_skills_is_just_provision_for_a_forgeless_workflow(client: TestClient) -> None:
    task_id = _new_task(client)
    resp = client.get(f"/tasks/{task_id}/skills")
    assert resp.status_code == 200
    # spike has no forge skills, but every task gets the agnostic `provision` skill (ADR 0011).
    assert [s["name"] for s in resp.json()] == ["provision"]


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


def test_set_turn_and_blocked(client: TestClient) -> None:
    task_id = _new_task(client)  # turn=agent, blocked=false
    turned = client.put(f"/tasks/{task_id}/turn", json={"turn": "user"})
    assert turned.status_code == 200
    assert turned.json()["turn"] == "user"

    blocked = client.put(f"/tasks/{task_id}/blocked", json={"blocked": True})
    assert blocked.json()["blocked"] is True
    assert blocked.json()["turn"] == "user"  # flip-independent: the block left the turn alone


def test_claim_release_over_rest(client: TestClient) -> None:
    task_id = _new_task(client)
    assert client.get(f"/tasks/{task_id}").json()["claimed_by"] is None

    claimed = client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-1"})
    assert claimed.status_code == 200 and claimed.json()["claimed_by"] == "host-1"

    # a different runner is refused with 409 while it's held
    assert client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-2"}).status_code == 409

    # release frees it; another runner can then claim
    assert client.delete(f"/tasks/{task_id}/claim").json()["claimed_by"] is None
    assert client.put(f"/tasks/{task_id}/claim", json={"runner_id": "host-2"}).json()["claimed_by"] == "host-2"


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


def test_set_state_bypasses_the_gate(gated_client: TestClient) -> None:
    task_id = gated_client.post("/tasks", json={"repo_id": "r1", "workflow": "gated"}).json()["id"]
    # The declared transition is gated (409); the user's free state-set overrides it.
    assert gated_client.post(f"/tasks/{task_id}/transition", json={"to_state": "COMPLETE"}).status_code == 409
    forced = gated_client.put(f"/tasks/{task_id}/state", json={"state": "COMPLETE"})
    assert forced.status_code == 200
    assert forced.json()["state"] == "COMPLETE"


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
