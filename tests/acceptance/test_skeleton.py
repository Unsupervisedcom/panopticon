"""Slice 1 acceptance: the walking skeleton, end to end over REST.

Proves the contract path: create a task -> the task service persists it -> a (fake)
container registers (liveness) -> sets a slug -> requests a transition the workflow accepts
-> history reflects it -> liveness is cleaned up. No Docker, no LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.sessionservice.stub_runner import StubRunner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def service_and_client(tmp_path: Path) -> Iterator[tuple[TaskService, TaskServiceClient]]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as http:
        yield service, TaskServiceClient(http)


@pytest.fixture
def client(service_and_client: tuple[TaskService, TaskServiceClient]) -> TaskServiceClient:
    _, c = service_and_client
    return c


def test_walking_skeleton(client: TaskServiceClient) -> None:
    # 1. A task is created and persisted in the workflow's initial state.
    task = client.create_task("r1", "spike")
    task_id = task["id"]
    assert task["state"] == "ITERATING"
    assert task["slug"] is None

    # 2-4. The runner "spawns" a container that registers, sets the slug, and works.
    runner = StubRunner(client)

    def work(c: TaskServiceClient, tid: str) -> None:
        c.put_artifact(tid, "plan.md", b"# Plan\nfix the widget\n")
        c.request_transition(tid, "COMPLETE", trigger="finish")

    runner.spawn(task_id, proposed_slug="fix-widget", work=work)

    # 5. The persisted record reflects everything the container did.
    final = client.get_task(task_id)
    assert final["slug"] == "fix-widget"  # slug was set in the container
    assert final["state"] == "COMPLETE"
    assert [h["to_state"] for h in final["history"]] == ["ITERATING", "COMPLETE"]
    assert client.get_artifact(task_id, "plan.md") == b"# Plan\nfix the widget\n"

    # Liveness registration was cleaned up on container exit.
    assert client.list_registrations(task_id) == []


def test_slug_hook_does_not_overwrite_existing_slug(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    client.set_slug(task_id, "chosen-by-user")
    StubRunner(client).spawn(task_id, proposed_slug="would-be-overwrite")
    assert client.get_task(task_id)["slug"] == "chosen-by-user"


def test_record_provisioning_over_rest(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]

    # Slug-gated: the branch is named from the slug, so the session service can't report
    # provisioning before one is set. The REST layer surfaces that as a 400.
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.record_provisioning(task_id, "panopticon/x", "/clones/x")
    assert exc.value.response.status_code == 400

    client.set_slug(task_id, "fix-widget")
    out = client.record_provisioning(task_id, "panopticon/fix-widget", f"/clones/{task_id}")
    assert (out["branch"], out["clone"]) == ("panopticon/fix-widget", f"/clones/{task_id}")
    assert client.get_task(task_id)["branch"] == "panopticon/fix-widget"  # persisted


def test_container_lifecycle_endpoints_drive_container_status_over_rest(
    client: TaskServiceClient,
) -> None:
    task = client.create_task("r1", "spike")
    task_id = task["id"]
    assert task["container_status"] == "queued"  # unclaimed, non-terminal
    assert task["lifecycle_detail"] is None

    client.claim(task_id, "host-1")
    # No runner-liveness connection is held in this sync test, so the claim reads as a runner that
    # isn't connected — disconnected (the new requirement), not a bare guess.
    assert client.get_task(task_id)["container_status"] == "disconnected"

    # The session service reports a spawn phase + detail; it surfaces in TaskOut.
    out = client.report_lifecycle(task_id, "host-1", "building", "gh + uv layers")
    assert out["lifecycle_detail"] == "gh + uv layers"

    # An open container registration trumps everything → live.
    reg = client.register(task_id, "c1", "host-1")
    assert client.get_task(task_id)["container_status"] == "live"
    client.deregister(reg["id"])

    # Clearing the reported phase (the daemon's down-detection) drops the detail again.
    cleared = client.clear_lifecycle(task_id)
    assert cleared["lifecycle_detail"] is None


def test_set_url_over_rest(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.get_task(task_id)["url"] is None  # unset on create
    out = client.set_url(task_id, "https://github.com/acme/widgets/pull/7")
    assert out["url"] == "https://github.com/acme/widgets/pull/7"
    assert client.get_task(task_id)["url"] == "https://github.com/acme/widgets/pull/7"  # persisted


def test_set_tokens_used_over_rest(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.get_task(task_id)["tokens_used"] is None  # unset on create
    out = client.set_tokens_used(task_id, 12750)
    assert out["tokens_used"] == 12750
    assert client.get_task(task_id)["tokens_used"] == 12750  # persisted


def test_set_token_estimate_over_rest(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.get_task(task_id)["token_estimate"] is None  # unset on create
    out = client.set_token_estimate(task_id, 500000)
    assert out["token_estimate"] == 500000
    assert client.get_task(task_id)["token_estimate"] == 500000  # persisted


def test_set_dependencies_over_rest(client: TaskServiceClient) -> None:
    task_a = client.create_task("r1", "spike")["id"]
    task_b = client.create_task("r1", "spike")["id"]

    assert client.get_task(task_a)["depends_on_task_ids"] == []  # empty on create

    # Set a dependency and verify it's returned and persisted.
    out = client.set_dependencies(task_a, [task_b])
    assert out["depends_on_task_ids"] == [task_b]
    assert client.get_task(task_a)["depends_on_task_ids"] == [task_b]

    # Replace with an empty list — clears all dependencies.
    out = client.set_dependencies(task_a, [])
    assert out["depends_on_task_ids"] == []
    assert client.get_task(task_a)["depends_on_task_ids"] == []


def test_set_dependencies_rejects_unknown_dep(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.set_dependencies(task_id, ["no-such-task"])
    assert exc.value.response.status_code == 404


def test_set_dependencies_rejects_self_reference(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.set_dependencies(task_id, [task_id])
    assert exc.value.response.status_code == 400


def test_depends_on_included_in_list_summary(client: TaskServiceClient) -> None:
    task_a = client.create_task("r1", "spike")["id"]
    task_b = client.create_task("r1", "spike")["id"]
    client.set_dependencies(task_a, [task_b])
    summaries = {t["id"]: t for t in client.list_tasks()}
    assert summaries[task_a]["depends_on_task_ids"] == [task_b]
    assert summaries[task_b]["depends_on_task_ids"] == []


def test_registration_active_during_work(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    seen: list[int] = []

    def work(c: TaskServiceClient, tid: str) -> None:
        seen.append(len(c.list_registrations(tid)))  # registered while working

    StubRunner(client).spawn(task_id, work=work)
    assert seen == [1]
    assert client.list_registrations(task_id) == []  # deregistered after


def test_list_tasks_returns_no_history(client: TaskServiceClient) -> None:
    # GET /tasks is cheap: it returns only tasks-table fields, no history.
    task_id = client.create_task("r1", "spike")["id"]
    listed = client.list_tasks()
    assert len(listed) == 1
    assert "history" not in listed[0]

    # GET /tasks/{id} still returns full detail including history.
    detail = client.get_task(task_id)
    assert "history" in detail
    assert len(detail["history"]) > 0


def test_governor_task_id_over_rest(client: TaskServiceClient) -> None:
    governor_id = client.create_task("r1", "spike")["id"]
    child_id = client.create_task("r1", "spike")["id"]

    # Unset on create by default.
    assert client.get_task(child_id)["governor_task_id"] is None

    # Create with governor_task_id set via the body.
    child2 = client._json(
        client._http.post(
            "/tasks", json={"repo_id": "r1", "workflow": "spike", "governor_task_id": governor_id}
        )
    )
    assert child2["governor_task_id"] == governor_id

    # Set/clear via PUT /tasks/{id}/governor.
    out = client.set_governor(child_id, governor_id)
    assert out["governor_task_id"] == governor_id
    assert client.get_task(child_id)["governor_task_id"] == governor_id  # persisted

    cleared = client.set_governor(child_id, None)
    assert cleared["governor_task_id"] is None


def test_list_tasks_terminal_filter(client: TaskServiceClient) -> None:
    # Create one active task and one complete task.
    active_id = client.create_task("r1", "spike")["id"]
    done_id = client.create_task("r1", "spike")["id"]
    client.request_transition(done_id, "COMPLETE", trigger="finish")

    http = client._http  # raw httpx client for query-param testing
    all_tasks = http.get("/tasks").json()
    assert len(all_tasks) == 2
    assert all("history" not in t for t in all_tasks)  # still no history

    active_only = http.get("/tasks", params={"terminal": "false"}).json()
    assert [t["id"] for t in active_only] == [active_id]

    terminal_only = http.get("/tasks", params={"terminal": "true"}).json()
    assert [t["id"] for t in terminal_only] == [done_id]


# -- cascade-drop: dropping a governor drops all non-terminal governed tasks -----


def _make_governed(client: TaskServiceClient, gov_id: str) -> str:
    """Create a spike task governed by gov_id; return its id."""
    return client._json(
        client._http.post(
            "/tasks", json={"repo_id": "r1", "workflow": "spike", "governor_task_id": gov_id}
        )
    )["id"]


def test_cascade_drop_governed_tasks(client: TaskServiceClient) -> None:
    gov_id = client.create_task("r1", "spike")["id"]
    child1_id = _make_governed(client, gov_id)
    child2_id = _make_governed(client, gov_id)

    client.request_transition(gov_id, "DROPPED", trigger="drop")

    assert client.get_task(gov_id)["state"] == "DROPPED"
    assert client.get_task(child1_id)["state"] == "DROPPED"
    assert client.get_task(child2_id)["state"] == "DROPPED"


def test_cascade_drop_recursive(client: TaskServiceClient) -> None:
    # Governor → child → grandchild; dropping governor cascades through all levels.
    gov_id = client.create_task("r1", "spike")["id"]
    child_id = _make_governed(client, gov_id)
    grandchild_id = _make_governed(client, child_id)

    client.request_transition(gov_id, "DROPPED", trigger="drop")

    assert client.get_task(gov_id)["state"] == "DROPPED"
    assert client.get_task(child_id)["state"] == "DROPPED"
    assert client.get_task(grandchild_id)["state"] == "DROPPED"


def test_cascade_drop_skips_already_terminal_governed(client: TaskServiceClient) -> None:
    # A governed task that is already COMPLETE before the governor drops stays COMPLETE.
    gov_id = client.create_task("r1", "spike")["id"]
    done_child_id = _make_governed(client, gov_id)
    active_child_id = _make_governed(client, gov_id)

    client.request_transition(done_child_id, "COMPLETE", trigger="advance")
    client.request_transition(gov_id, "DROPPED", trigger="drop")

    assert client.get_task(done_child_id)["state"] == "COMPLETE"  # untouched
    assert client.get_task(active_child_id)["state"] == "DROPPED"  # cascaded


def test_cascade_drop_via_set_state(client: TaskServiceClient) -> None:
    # cascade-drop also fires when the governor is dropped via set_state (free move).
    gov_id = client.create_task("r1", "spike")["id"]
    child_id = _make_governed(client, gov_id)

    client._http.put(f"/tasks/{gov_id}/state", json={"state": "DROPPED"})

    assert client.get_task(gov_id)["state"] == "DROPPED"
    assert client.get_task(child_id)["state"] == "DROPPED"


# -- runner host tracking (M5.3) -----------------------------------------------


def test_get_runners_returns_id_and_host(
    service_and_client: tuple[TaskService, TaskServiceClient],
) -> None:
    # GET /runners returns [{id, host}] objects; empty when no runner is connected.
    svc, client = service_and_client
    assert client.live_runners() == []

    # Register a runner directly on the service (avoiding HTTP streaming in TestClient).
    reg = asyncio.run(svc.register_runner("host-1", host="box.example.com"))
    assert client.live_runners() == [{"id": "host-1", "host": "box.example.com"}]

    asyncio.run(svc.deregister_runner(reg.id))
    assert client.live_runners() == []  # gone after deregistration


def test_get_runner_by_id_returns_host(
    service_and_client: tuple[TaskService, TaskServiceClient],
) -> None:
    # GET /runners/{id} returns the single runner's {id, host}; 404 when not connected.
    svc, client = service_and_client
    assert client.get_runner("host-1") is None  # not yet connected

    reg = asyncio.run(svc.register_runner("host-1", host="box.example.com"))
    assert client.get_runner("host-1") == {"id": "host-1", "host": "box.example.com"}

    asyncio.run(svc.deregister_runner(reg.id))
    assert client.get_runner("host-1") is None  # gone after deregistration


def test_task_out_runner_host_reflects_claiming_runner_host(
    service_and_client: tuple[TaskService, TaskServiceClient],
) -> None:
    # TaskOut.runner_host is derived from claimed_by → runner registration host at query time.
    svc, client = service_and_client
    task_id = client.create_task("r1", "spike")["id"]
    assert client.get_task(task_id)["runner_host"] is None  # unclaimed

    reg = asyncio.run(svc.register_runner("host-1", host="myhost.local"))
    client.claim(task_id, "host-1")
    assert client.get_task(task_id)["runner_host"] == "myhost.local"
    # Also surfaced in list summary.
    summary = next(t for t in client.list_tasks() if t["id"] == task_id)
    assert summary["runner_host"] == "myhost.local"

    asyncio.run(svc.deregister_runner(reg.id))
    assert client.get_task(task_id)["runner_host"] is None  # runner gone → host unknown
