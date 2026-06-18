"""Host-side spawn loop (ADR 0008): claim an unclaimed task, then spawn its container. Unit tests
drive `spawn_one`/`spawnable_tasks` with fakes; an integration test runs against the real task
service over REST. No Docker, no LLM — `git`/runner are fakes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.core.models import Repo
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawner import Spawner, spawnable_tasks
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _no_op_run(args: object, *, check: bool = True) -> str:
    return ""


class _FakeRunner:
    """Records spawn calls; stands in for LocalRunner."""

    def __init__(self) -> None:
        self.spawned: list[dict[str, object]] = []

    def spawn(self, task_id: str, *, env_file: str | None = None, creds_volume: str | None = None, workspace: str | None = None, image: str | None = None) -> str:
        self.spawned.append({"task_id": task_id, "env_file": env_file, "creds_volume": creds_volume, "workspace": workspace, "image": image})
        return f"panopticon-{task_id}"


class _FakeClient:
    """Captures claims; serves one repo. `claim` 409s when already held by another runner."""

    def __init__(self, *, repo: JsonObj, image_layer: str = "") -> None:
        self._repo = repo
        self._image_layer = image_layer
        self.claims: list[tuple[str, str]] = []
        self._held_by: dict[str, str] = {}

    def workflow_image_layer(self, name: str) -> str:
        return self._image_layer

    def claim(self, task_id: str, runner_id: str) -> JsonObj:
        holder = self._held_by.get(task_id)
        if holder not in (None, runner_id):
            request = httpx.Request("PUT", f"http://svc/tasks/{task_id}/claim")
            raise httpx.HTTPStatusError("conflict", request=request, response=httpx.Response(409, request=request))
        self._held_by[task_id] = runner_id
        self.claims.append((task_id, runner_id))
        return {"id": task_id, "claimed_by": runner_id}

    def hold(self, task_id: str, runner_id: str) -> None:
        self._held_by[task_id] = runner_id  # simulate another runner already holding it

    def get_repo(self, repo_id: str) -> JsonObj:
        return self._repo


def _spawner(client: object, runner: object) -> Spawner:
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True)  # type: ignore[arg-type]
    return Spawner(client, runner, runner_id="host-1", cache=cache, tasks_root="/tasks", git=GitClones(run=_no_op_run))  # type: ignore[arg-type]


_REPO: JsonObj = {"id": "r1", "git_url": "https://forge/r1.git", "env_file": "/sec/r1.env", "creds_volume": "creds-r1"}


def test_spawn_one_claims_then_spawns_a_fresh_task() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    cid = _spawner(client, runner).spawn_one({"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": None})
    assert cid == "panopticon-t1"
    assert client.claims == [("t1", "host-1")]  # claimed for this host first
    assert runner.spawned[0]["workspace"] == "/tasks/t1"  # per-task clone mounted
    assert runner.spawned[0]["env_file"] == "/sec/r1.env" and runner.spawned[0]["creds_volume"] == "creds-r1"
    assert runner.spawned[0]["image"] is None  # spike has no image layer → runner uses the base


class _FakeImageBuilder:
    """Records compose calls; stands in for ImageBuilder (no docker)."""

    def __init__(self) -> None:
        self.built: list[tuple[str, str, list[str]]] = []

    def build(self, workflow: str, repo_id: str, layers: list[str]) -> str:
        self.built.append((workflow, repo_id, layers))
        return f"panopticon-{workflow}-{repo_id}"


def test_spawn_one_composes_the_workflow_image_when_it_has_a_layer() -> None:
    client, runner = _FakeClient(repo=_REPO, image_layer="RUN apt-get install --yes gh"), _FakeRunner()
    images = _FakeImageBuilder()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True)  # type: ignore[arg-type]
    spawner = Spawner(
        client, runner, runner_id="host-1", cache=cache, tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run), images=images,  # type: ignore[arg-type]
    )
    spawner.spawn_one({"id": "t1", "repo_id": "r1", "workflow": "parity", "state": "PLANNING", "claimed_by": None})
    assert images.built == [("parity", "r1", ["RUN apt-get install --yes gh"])]  # composed base → layer
    assert runner.spawned[0]["image"] == "panopticon-parity-r1"  # spawned on the composed image


def test_spawn_one_skips_terminal_and_already_claimed_tasks() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    spawner = _spawner(client, runner)
    assert spawner.spawn_one({"id": "t1", "repo_id": "r1", "state": "COMPLETE", "claimed_by": None}) is None
    assert spawner.spawn_one({"id": "t2", "repo_id": "r1", "state": "ITERATING", "claimed_by": "host-9"}) is None
    assert client.claims == [] and runner.spawned == []


def test_spawn_one_skips_when_another_runner_wins_the_claim() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    client.hold("t1", "host-2")  # another runner grabbed it between our snapshot and claim
    assert _spawner(client, runner).spawn_one({"id": "t1", "repo_id": "r1", "state": "ITERATING", "claimed_by": None}) is None
    assert runner.spawned == []  # 409 → no spawn


def test_spawnable_tasks_filters_unclaimed_non_terminal() -> None:
    class _Lister:
        def list_tasks(self) -> list[JsonObj]:
            return [
                {"id": "a", "state": "ITERATING", "claimed_by": None},  # spawnable
                {"id": "b", "state": "ITERATING", "claimed_by": "host-1"},  # already claimed
                {"id": "c", "state": "COMPLETE", "claimed_by": None},  # terminal
            ]

    assert [t["id"] for t in spawnable_tasks(_Lister())()] == ["a"]  # type: ignore[arg-type]


def test_spawner_against_the_real_service(tmp_path: Path) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git"))
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        runner = _FakeRunner()
        spawner = Spawner(
            client, runner, runner_id="host-1",  # type: ignore[arg-type]
            cache=CloneCache("/cache", run=_no_op_run, exists=lambda _p: True), tasks_root="/tasks",
            git=GitClones(run=_no_op_run),
        )
        (task,) = spawnable_tasks(client)()  # the fresh task is spawnable
        assert spawner.spawn_one(task) == f"panopticon-{task_id}"
        assert client.get_task(task_id)["claimed_by"] == "host-1"  # claim recorded on the service
        assert spawnable_tasks(client)() == []  # now claimed → no longer spawnable
