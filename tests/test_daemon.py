"""The observe-and-provision loop (ADR 0010/0011): the session service polls the tasks it runs and
branches each per-task clone once it acquires a slug. Unit tests drive the loop with fakes; an
integration test runs it against the real task service over REST. No Docker, no LLM."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.core.models import Repo
from panopticon.sessionservice.daemon import ProvisionDaemon, run_daemon, watched_tasks
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

    def provision(self, task: JsonObj) -> str | None:
        self.seen.append(task["id"])
        result = self._results.get(task["id"])
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


def test_tick_provisions_watched_tasks_and_returns_their_branches() -> None:
    client = _FakeClient({"t1": {"id": "t1"}, "t2": {"id": "t2"}})
    provisioner = _FakeProvisioner({"t1": "panopticon/a", "t2": None})  # t2 not ready
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1", "t2"])  # type: ignore[arg-type]

    assert daemon.tick() == ["panopticon/a"]  # only the provisioned one
    assert provisioner.seen == ["t1", "t2"]  # but both were considered


def test_tick_isolates_a_failing_task_from_the_others() -> None:
    client = _FakeClient({"t1": {"id": "t1"}, "t2": {"id": "t2"}})
    provisioner = _FakeProvisioner({"t1": RuntimeError("git blew up"), "t2": "panopticon/b"})
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1", "t2"])  # type: ignore[arg-type]

    assert daemon.tick() == ["panopticon/b"]  # t1's error is logged + skipped; t2 still provisions
    assert provisioner.seen == ["t1", "t2"]


def test_tick_skips_an_already_provisioned_task() -> None:
    client = _FakeClient({"t1": {"id": "t1", "provisioned": True}})
    provisioner = _FakeProvisioner({})
    daemon = ProvisionDaemon(client, provisioner, lambda: ["t1"])  # type: ignore[arg-type]

    assert daemon.tick() == []
    assert provisioner.seen == []  # provision is never called for a provisioned task


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
    """The loop branches a task the moment it observes the slug, then no-ops — end to end over
    REST. `git` is faked; the branch + clone path land on the real task service."""
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(
        Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git", default_base="trunk")
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]

        def fake_run(args: object, *, check: bool = True) -> str:
            return ""

        provisioner = Provisioner(client, clones_root="/clones", git=GitClones(run=fake_run))  # type: ignore[arg-type]
        daemon = ProvisionDaemon(client, provisioner, lambda: [task_id], sleep=lambda _s: None)

        # Pass 1: no slug yet → nothing provisioned.
        assert daemon.tick() == []
        assert client.get_task(task_id)["branch"] is None

        # The agent sets the slug; the next pass observes it and branches.
        client.set_slug(task_id, "fix-widget")
        assert daemon.tick() == ["panopticon/fix-widget"]
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == f"/clones/{task_id}"  # the per-task clone path

        # Pass 3: already branched → no-op.
        assert daemon.tick() == []


def test_watched_tasks_lists_only_unprovisioned(tmp_path: Path) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git"))
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        unprovisioned = client.create_task("r1", "spike")["id"]
        done = client.create_task("r1", "spike")["id"]
        client.set_slug(done, "fix-widget")
        client.record_provisioning(done, "panopticon/fix-widget", f"/clones/{done}")
        # the provisioned task drops out of the watch set; only the unprovisioned one remains
        assert watched_tasks(client)() == [unprovisioned]


def test_run_daemon_provisions_a_slugged_task_over_one_pass(tmp_path: Path) -> None:
    """The launch path (`run_daemon`) wires the provisioner + watch-set and branches a slugged
    task — end to end over REST, `git` faked, stopped after one pass."""
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git", default_base="trunk"))
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        client.set_slug(task_id, "fix-widget")

        def fake_run(args: object, *, check: bool = True) -> str:
            return ""

        passes = {"n": 0}

        def until() -> bool:  # one pass, then stop
            done = passes["n"] >= 1
            passes["n"] += 1
            return done

        run_daemon(
            client, tasks_root="/clones", git=GitClones(run=fake_run), until=until, sleep=lambda _s: None
        )
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == f"/clones/{task_id}"
