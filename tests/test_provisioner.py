"""Host-side provisioning (ADR 0010): the session service ensures the repo's clone, creates the
slug-named worktree, and records it on the task service. Unit tests pin the emitted `git`; an
integration test drives the real task service over REST. No Docker, no LLM — `git` is a fake."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitWorktrees
from panopticon.core.models import Repo
from panopticon.sessionservice.clones import CloneCache
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


def _provisioner(client: object, run: Callable[..., str], *, cloned: bool = False) -> Provisioner:
    """Build a Provisioner whose clone cache + git share the recording ``run``. ``cloned`` picks
    whether the repo's clone already exists (fetch) or not (clone)."""
    cache = CloneCache("/clones", run=run, exists=lambda _p: cloned)  # type: ignore[arg-type]
    return Provisioner(
        client,  # type: ignore[arg-type]
        cache,
        worktrees_root="/wt",
        git=GitWorktrees(run=run),  # type: ignore[arg-type]
    )


class _FakeClient:
    """A task-service client stub: serves one repo, captures record_provisioning calls."""

    def __init__(self, *, default_base: str, git_url: str = "https://x/r1.git") -> None:
        self._repo: JsonObj = {"id": "r1", "default_base": default_base, "git_url": git_url}
        self.recorded: list[tuple[str, str, str]] = []

    def get_repo(self, repo_id: str) -> JsonObj:
        return self._repo

    def record_provisioning(self, task_id: str, branch: str, worktree: str) -> JsonObj:
        self.recorded.append((task_id, branch, worktree))
        return {"id": task_id, "branch": branch, "worktree": worktree}


def test_provisions_a_ready_task_ensuring_the_clone_first() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = _provisioner(client, run, cloned=False)  # repo not cloned yet

    worktree = provisioner.provision(
        {"id": "t1", "repo_id": "r1", "slug": "fix-widget", "worktree": None}
    )

    assert worktree is not None
    assert (worktree.branch, worktree.path) == ("panopticon/fix-widget", "/wt/r1/panopticon/fix-widget")
    assert calls == [
        ["git", "clone", "https://x/r1.git", "/clones/r1"],  # ensure the clone…
        ["git", "-C", "/clones/r1", "worktree", "add", "-b",
         "panopticon/fix-widget", "/wt/r1/panopticon/fix-widget", "trunk"],  # …then the worktree off base
    ]
    assert client.recorded == [("t1", "panopticon/fix-widget", "/wt/r1/panopticon/fix-widget")]


def test_skips_a_task_without_a_slug() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = _provisioner(client, run)

    assert provisioner.provision({"id": "t1", "repo_id": "r1", "slug": None, "worktree": None}) is None
    assert calls == []  # no git at all — not even the clone
    assert client.recorded == []


def test_skips_an_already_provisioned_task() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(default_base="trunk")
    provisioner = _provisioner(client, run, cloned=True)

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
        provisioner = _provisioner(client, run, cloned=False)

        worktree = provisioner.provision(client.get_task(task_id))
        assert worktree is not None and worktree.branch == "panopticon/fix-widget"
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["worktree"] == "/wt/r1/panopticon/fix-widget"
        assert len(calls) == 2  # clone + worktree add

        # A second pass sees the recorded worktree and does nothing — no new git, no re-record.
        assert provisioner.provision(client.get_task(task_id)) is None
        assert len(calls) == 2
