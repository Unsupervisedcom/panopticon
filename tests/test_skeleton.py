"""Slice 1 acceptance: the walking skeleton, end to end over REST.

Proves the contract path: create a task -> the task service persists it -> a (fake)
container registers (liveness) -> sets a slug -> requests a transition the workflow accepts
-> history reflects it -> liveness is cleaned up. No Docker, no LLM.
"""

from __future__ import annotations

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
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.service import TaskService
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TaskServiceClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


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


def test_registration_active_during_work(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    seen: list[int] = []

    def work(c: TaskServiceClient, tid: str) -> None:
        seen.append(len(c.list_registrations(tid)))  # registered while working

    StubRunner(client).spawn(task_id, work=work)
    assert seen == [1]
    assert client.list_registrations(task_id) == []  # deregistered after
