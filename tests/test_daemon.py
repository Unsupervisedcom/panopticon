"""The observe-and-provision loop (ADR 0010): the session service polls the tasks it runs and
provisions each once it acquires a slug. Unit tests drive the loop with fakes; an integration test
runs it against the real task service over REST. No Docker, no LLM."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitWorktrees, Worktree
from panopticon.core.models import Repo
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.daemon import ProvisionDaemon
from panopticon.sessionservice.provisioner import Provisioner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


class _FakeClient:
    """Serves tasks by id from a dict (mutate it between passes to simulate state changing)."""

    def __init__(self, tasks: dict[str, JsonObj]) -> None:
        self.tasks = tasks

    def get_task(self, task_id: str) -> JsonObj:
        return self.tasks[task_id]


class _FakeProvisioner:
    """Records the tasks it was asked to provision; returns a scripted result per task id."""

    def __init__(self, results: dict[str, object]) -> None:
        self._results = results
        self.seen: list[str] = []

    def provision(self, task: JsonObj) -> Worktree | None:
        self.seen.append(task["id"])
        result = self._results.get(task["id"])
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


def test_tick_provisions_watched_tasks_and_returns_their_worktrees() -> None:
    wt = Worktree(branch="panopticon/a", path="/wt/r1/panopticon/a")
    client = _FakeClient({"t1": {"id": "t1"}, "t2": {"id": "t2"}})
    provisioner = _FakeProvisioner({"t1": wt, "t2": None})  # t2 not ready
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1", "t2"])  # type: ignore[arg-type]

    assert daemon.tick() == [wt]  # only the provisioned one
    assert provisioner.seen == ["t1", "t2"]  # but both were considered


def test_tick_isolates_a_failing_task_from_the_others() -> None:
    wt = Worktree(branch="panopticon/b", path="/wt/r1/panopticon/b")
    client = _FakeClient({"t1": {"id": "t1"}, "t2": {"id": "t2"}})
    provisioner = _FakeProvisioner({"t1": RuntimeError("git blew up"), "t2": wt})
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1", "t2"])  # type: ignore[arg-type]

    assert daemon.tick() == [wt]  # t1's error is logged + skipped; t2 still provisions
    assert provisioner.seen == ["t1", "t2"]


def test_run_polls_until_the_stop_condition() -> None:
    client = _FakeClient({"t1": {"id": "t1"}})
    provisioner = _FakeProvisioner({"t1": None})
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1"], sleep=lambda _s: None)  # type: ignore[arg-type]

    passes = {"n": 0}

    def until() -> bool:  # stop after two passes
        done = passes["n"] >= 2
        passes["n"] += 1
        return done

    daemon.run(until=until)
    assert provisioner.seen == ["t1", "t1"]  # ticked exactly twice


def test_daemon_against_the_real_service(tmp_path: Path) -> None:
    """The loop provisions a task the moment it observes the slug, then no-ops — end to end over
    REST. `git` is faked; the worktree ref lands on the real task service."""
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(
        Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git", default_base="trunk")
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]

        def fake_run(args: object, *, check: bool = True) -> str:
            return ""

        provisioner = Provisioner(
            client,
            CloneCache("/clones", run=fake_run, exists=lambda _p: True),  # type: ignore[arg-type]
            worktrees_root="/wt",
            git=GitWorktrees(run=fake_run),  # type: ignore[arg-type]
        )
        daemon = ProvisionDaemon(client, provisioner, lambda: [task_id], sleep=lambda _s: None)

        # Pass 1: no slug yet → nothing provisioned.
        assert daemon.tick() == []
        assert client.get_task(task_id)["worktree"] is None

        # The agent sets the slug; the next pass observes it and provisions.
        client.set_slug(task_id, "fix-widget")
        provisioned = daemon.tick()
        assert [w.branch for w in provisioned] == ["panopticon/fix-widget"]
        assert client.get_task(task_id)["worktree"] == "/wt/r1/panopticon/fix-widget"

        # Pass 3: already provisioned → no-op.
        assert daemon.tick() == []
