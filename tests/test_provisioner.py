"""Host-side provisioning (ADR 0010): the session service creates the slug-named worktree and
records it on the task service. Unit tests pin the emitted `git`; an integration test drives the
real task service over REST. No Docker, no LLM — `git` is a fake command-runner."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitWorktrees
from panopticon.core.models import Repo
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _recording_runner() -> tuple[list[list[str]], object]:
    """A fake git command-runner that captures the argv of each invocation."""
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return ""

    return calls, run


class _FakeClient:
    """A task-service client stub: serves one repo, captures record_provisioning calls."""

    def __init__(self, default_base: str) -> None:
        self._default_base = default_base
        self.recorded: list[tuple[str, str, str]] = []

    def get_repo(self, repo_id: str) -> JsonObj:
        return {"id": repo_id, "default_base": self._default_base}

    def record_provisioning(self, task_id: str, branch: str, worktree: str) -> JsonObj:
        self.recorded.append((task_id, branch, worktree))
        return {"id": task_id, "branch": branch, "worktree": worktree}


def test_provisions_a_ready_task_and_records_it() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = Provisioner(
        client,  # type: ignore[arg-type]
        clones_root="/clones",
        worktrees_root="/wt",
        git=GitWorktrees(run=run),  # type: ignore[arg-type]
    )

    worktree = provisioner.provision(
        {"id": "t1", "repo_id": "r1", "slug": "fix-widget", "worktree": None}
    )

    assert worktree is not None
    assert (worktree.branch, worktree.path) == ("panopticon/fix-widget", "/wt/r1/panopticon/fix-widget")
    assert calls == [
        ["git", "-C", "/clones/r1", "worktree", "add", "-b",
         "panopticon/fix-widget", "/wt/r1/panopticon/fix-widget", "trunk"],  # off the repo's base
    ]
    assert client.recorded == [("t1", "panopticon/fix-widget", "/wt/r1/panopticon/fix-widget")]


def test_skips_a_task_without_a_slug() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = Provisioner(
        client, clones_root="/clones", worktrees_root="/wt", git=GitWorktrees(run=run),  # type: ignore[arg-type]
    )

    assert provisioner.provision({"id": "t1", "repo_id": "r1", "slug": None, "worktree": None}) is None
    assert calls == []  # no git
    assert client.recorded == []  # nothing recorded


def test_skips_an_already_provisioned_task() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = Provisioner(
        client, clones_root="/clones", worktrees_root="/wt", git=GitWorktrees(run=run),  # type: ignore[arg-type]
    )

    already = {"id": "t1", "repo_id": "r1", "slug": "fix-widget", "worktree": "/wt/r1/panopticon/fix-widget"}
    assert provisioner.provision(already) is None  # idempotent: the worktree is already recorded
    assert calls == []
    assert client.recorded == []


def test_provisioner_against_the_real_service(tmp_path: Path) -> None:
    """End to end against the real task service over REST: provisioning is recorded and the
    second pass is a no-op (the pull loop can call it repeatedly)."""
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(
        Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git", default_base="trunk")
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        client.set_slug(task_id, "fix-widget")

        calls, run = _recording_runner()
        provisioner = Provisioner(
            client, clones_root="/clones", worktrees_root="/wt", git=GitWorktrees(run=run),  # type: ignore[arg-type]
        )

        worktree = provisioner.provision(client.get_task(task_id))
        assert worktree is not None and worktree.branch == "panopticon/fix-widget"
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["worktree"] == "/wt/r1/panopticon/fix-widget"

        # A second pass sees the recorded worktree and does nothing — no new git, no re-record.
        assert provisioner.provision(client.get_task(task_id)) is None
        assert len(calls) == 1
