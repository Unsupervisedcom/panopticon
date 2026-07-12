"""Host-side provisioning (ADR 0011): the session service branches the per-task clone and records
it on the task service. Unit tests pin the emitted `git`; an integration test drives the real task
service over REST. No Docker, no LLM — `git` is a fake command-runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.core.models import Repo
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _recording_runner() -> tuple[list[list[str]], Callable[..., str]]:
    """A fake git command-runner that captures the argv of each invocation."""
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return ""

    return calls, run


def _provisioner(client: object, run: Callable[..., str]) -> Provisioner:
    return Provisioner(client, clones_root="/clones", git=GitClones(run=run))  # type: ignore[arg-type]


class _FakeClient:
    """A task-service client stub: serves one repo, captures record_provisioning calls."""

    def __init__(
        self, *, git_url: str = "https://forge/r1.git", runner_type: str = "docker"
    ) -> None:
        self._repo: JsonObj = {"id": "r1", "default_base": "trunk", "git_url": git_url}
        self._runner_type = runner_type
        self.recorded: list[tuple[str, str, str]] = []

    def get_repo(self, repo_id: str) -> JsonObj:
        return self._repo

    def workflow_execution(self, name: str) -> JsonObj:
        return {
            "runner_type": self._runner_type,
            "script": "",
            "clone_repo": False,
            "workdir": None,
        }

    def record_provisioning(self, task_id: str, branch: str, clone: str) -> JsonObj:
        self.recorded.append((task_id, branch, clone))
        return {"id": task_id, "branch": branch, "clone": clone}


def test_provisions_a_ready_task_by_branching_the_clone() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()
    provisioner = _provisioner(client, run)

    branch = provisioner.provision(
        {"id": "t1", "repo_id": "r1", "slug": "fix-widget", "provisioned": False}
    )

    assert branch == "panopticon/fix-widget"
    # only branches — origin was pointed at the forge at spawn-prep (see test_spawn), not here
    assert calls == [["git", "-C", "/clones/t1", "checkout", "-b", "panopticon/fix-widget"]]
    assert client.recorded == [("t1", "panopticon/fix-widget", "/clones/t1")]


def test_skips_a_task_without_a_slug() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()
    provisioner = _provisioner(client, run)

    assert (
        provisioner.provision({"id": "t1", "repo_id": "r1", "slug": None, "provisioned": False})
        is None
    )
    assert calls == []  # no git
    assert client.recorded == []  # nothing recorded


def test_skips_an_already_provisioned_task() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()
    provisioner = _provisioner(client, run)

    already = {"id": "t1", "repo_id": "r1", "slug": "fix-widget", "provisioned": True}
    assert provisioner.provision(already) is None  # idempotent: already provisioned
    assert calls == []
    assert client.recorded == []


def test_skips_a_shell_workflow_task_which_has_no_clone() -> None:
    # A `runner_type = "shell"` task runs on the host with no per-task clone, so even with a slug
    # there is nothing to branch — the no-clone guarantee holds rather than failing on a missing dir.
    calls, run = _recording_runner()
    client = _FakeClient(runner_type="shell")
    provisioner = _provisioner(client, run)

    shell_task = {
        "id": "t1",
        "repo_id": "r1",
        "workflow": "setup-token",
        "slug": "mint",
        "provisioned": False,
    }
    assert provisioner.provision(shell_task) is None
    assert calls == []  # no git checkout -b against a nonexistent clone
    assert client.recorded == []


def test_provisioner_against_the_real_service(tmp_path: Path) -> None:
    """End to end against the real task service over REST: provisioning records the branch + clone
    path, and the second pass is a no-op (the pull loop can call it repeatedly)."""
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git", default_base="trunk")
        )
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        client.set_slug(task_id, "fix-widget")

        calls, run = _recording_runner()
        provisioner = Provisioner(client, clones_root="/clones", git=GitClones(run=run))

        branch = provisioner.provision(client.get_task(task_id))
        assert branch == "panopticon/fix-widget"
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == f"/clones/{task_id}"  # the per-task clone path
        assert len(calls) == 1  # checkout -b only (origin set at spawn-prep)

        # A second pass sees the recorded branch and does nothing — no new git, no re-record.
        assert provisioner.provision(client.get_task(task_id)) is None
        assert len(calls) == 1  # still just the one checkout -b from the first pass
