"""The shared task-service REST client (``panopticon.client.TaskServiceClient``) against the
real FastAPI app via ``TestClient`` — the surface both the container harness and the terminal
controller depend on. (The end-to-end liveness/registration path is in test_skeleton.py.)"""

from __future__ import annotations

import asyncio
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
    service = TaskService(
        SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


def test_reads_workflows_repos_and_tasks(client: TaskServiceClient) -> None:
    assert [w["name"] for w in client.list_workflows()] == ["spike"]
    assert [r["id"] for r in client.list_repos()] == ["r1"]
    task = client.create_task("r1", "spike")
    listed = client.list_tasks()
    assert [t["id"] for t in listed] == [task["id"]]
    assert client.get_task(task["id"])["state"] == "ITERATING"


def test_list_tasks_versioned_returns_snapshot_and_cursor(client: TaskServiceClient) -> None:
    tasks, version = client.list_tasks_versioned()
    assert tasks == [] and version == 0  # nothing written yet

    task = client.create_task("r1", "spike")
    tasks, bumped = client.list_tasks_versioned()
    assert [t["id"] for t in tasks] == [task["id"]]
    assert bumped > version

    # A ?wait with the now-stale cursor returns immediately (the version already moved past it),
    # so this doesn't block the sync client; the real long-poll/block path is in test_change_feed.
    tasks, again = client.list_tasks_versioned(since=version, wait=5)
    assert [t["id"] for t in tasks] == [task["id"]] and again == bumped


def test_drives_slug_and_transition(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    client.set_slug(task_id, "fix-widget")
    assert client.list_transitions(task_id) == ["COMPLETE", "DROPPED"]
    done = client.request_transition(task_id, "COMPLETE", trigger="finish")
    assert done["state"] == "COMPLETE"
    assert client.get_task(task_id)["slug"] == "fix-widget"


def test_drives_turn_and_blocked(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.set_turn(task_id, "user")["turn"] == "user"
    blocked = client.set_blocked(task_id, True)
    assert blocked["blocked"] is True and blocked["turn"] == "user"  # block preserves the turn


def test_drives_core_operations(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert client.list_operations(task_id) == {"advance": "COMPLETE", "drop": "DROPPED"}
    done = client.apply_operation(task_id, "advance")
    assert done["state"] == "COMPLETE"


def test_lists_states_and_sets_state_freely(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    assert set(client.list_states(task_id)) == {"ITERATING", "COMPLETE", "DROPPED"}
    assert client.set_state(task_id, "COMPLETE")["state"] == "COMPLETE"


def test_create_repo_over_rest(client: TaskServiceClient) -> None:
    client.create_repo("r2", "acme/other", "https://x/r2.git")
    assert {r["id"] for r in client.list_repos()} == {"r1", "r2"}


def test_create_repo_with_secret_references(
    client: TaskServiceClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env_file is a name under the secrets dir (#291), validated to exist on create (ADR 0007).
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "r3.env").write_text("ANTHROPIC_API_KEY=sk-test\n")
    repo = client.create_repo(
        "r3",
        "acme/svc",
        "https://x/r3.git",
        env_file="r3.env",
    )
    assert repo["env_file"] == "r3.env"


def test_update_repo_patches_only_sent_fields(client: TaskServiceClient) -> None:
    client.create_repo("r4", "acme/svc", "https://x/r4.git")
    updated = client.update_repo("r4", name="renamed", git_url="https://x/r4-new.git")
    assert updated["name"] == "renamed"
    assert updated["git_url"] == "https://x/r4-new.git"
    assert updated["default_base"] == "main"  # not sent → unchanged
    assert client.get_repo("r4")["name"] == "renamed"  # persisted


def test_create_repo_carries_capabilities(client: TaskServiceClient) -> None:
    # The dashboard's privileged-docker toggle creates a repo with docker_in_docker set.
    client.create_repo("r6", "svc", "https://x/r6.git", capabilities={"docker_in_docker": True})
    assert client.get_repo("r6")["capabilities"] == {"docker_in_docker": True}  # persisted


def test_update_repo_preserves_image_layer_and_capabilities(client: TaskServiceClient) -> None:
    # POST the full repo (incl. extras), then PATCH only a core field: the extras must survive.
    client._http.post(  # the client's create_repo doesn't carry image_layer_file; go raw for the seed
        "/repos",
        json={
            "id": "r5",
            "name": "svc",
            "git_url": "https://x/r5.git",
            "image_layer_file": "r5.layer",
            "capabilities": {"docker_in_docker": True},
        },
    ).raise_for_status()
    client.update_repo("r5", name="svc-2")
    got = client.get_repo("r5")
    assert got["name"] == "svc-2"
    assert got["image_layer_file"] == "r5.layer"  # the anti-footgun: PATCH left it intact
    assert got["capabilities"] == {"docker_in_docker": True}


def test_update_unknown_repo_404(client: TaskServiceClient) -> None:
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.update_repo("ghost", name="x")
    assert exc.value.response.status_code == 404


def test_update_repo_rejects_id_change(client: TaskServiceClient) -> None:
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.update_repo("r1", id="r1-renamed")
    assert exc.value.response.status_code == 400
