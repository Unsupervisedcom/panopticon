"""The observe-and-provision loop (ADR 0010/0011): the session service blocks on the task service's
change feed and branches each per-task clone once it acquires a slug. Unit tests drive the loop with
fakes; an integration test runs it against the real task service over REST. No Docker, no LLM."""

from __future__ import annotations

import asyncio
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


class _FakeFeed:
    """A change feed that yields a scripted snapshot per `list_tasks_versioned` call.

    Each entry is the full task list the loop wakes on; the version just increments so the daemon's
    `since` plumbing is exercised. Once the script is exhausted it returns the last snapshot again.
    """

    def __init__(self, snapshots: list[list[JsonObj]]) -> None:
        self._snapshots = snapshots
        self.sinces: list[int] = []
        self._n = 0

    def list_tasks_versioned(
        self, *, since: int = 0, wait: float | None = None
    ) -> tuple[list[JsonObj], int]:
        self.sinces.append(since)
        snapshot = self._snapshots[min(self._n, len(self._snapshots) - 1)]
        self._n += 1
        return snapshot, self._n


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


def test_provision_branches_watched_tasks_and_returns_their_branches() -> None:
    provisioner = _FakeProvisioner({"t1": "panopticon/a", "t2": None})  # t2 not ready (no slug yet)
    daemon = ProvisionDaemon(_FakeFeed([]), provisioner)  # type: ignore[arg-type]

    branches = daemon.provision(
        [{"id": "t1", "provisioned": False}, {"id": "t2", "provisioned": False}]
    )
    assert branches == ["panopticon/a"]  # only the provisioned one
    assert provisioner.seen == ["t1", "t2"]  # but both were considered


def test_provision_isolates_a_failing_task_from_the_others() -> None:
    provisioner = _FakeProvisioner({"t1": RuntimeError("git blew up"), "t2": "panopticon/b"})
    daemon = ProvisionDaemon(_FakeFeed([]), provisioner)  # type: ignore[arg-type]

    branches = daemon.provision(
        [{"id": "t1", "provisioned": False}, {"id": "t2", "provisioned": False}]
    )
    assert branches == ["panopticon/b"]  # t1's error is logged + skipped; t2 still provisions
    assert provisioner.seen == ["t1", "t2"]


def test_provision_skips_an_already_provisioned_task() -> None:
    provisioner = _FakeProvisioner({})
    daemon = ProvisionDaemon(_FakeFeed([]), provisioner)  # type: ignore[arg-type]

    assert daemon.provision([{"id": "t1", "provisioned": True}]) == []
    assert provisioner.seen == []  # the provisioned task is filtered out of the watch set


def test_run_blocks_on_the_feed_until_the_stop_condition() -> None:
    feed = _FakeFeed([[{"id": "t1", "provisioned": False}]])
    provisioner = _FakeProvisioner({"t1": None})
    daemon = ProvisionDaemon(feed, provisioner)  # type: ignore[arg-type]

    passes = {"n": 0}

    def until() -> bool:  # stop after two passes
        done = passes["n"] >= 2
        passes["n"] += 1
        return done

    daemon.run(until=until)
    assert provisioner.seen == ["t1", "t1"]  # woke + provisioned exactly twice
    assert feed.sinces == [0, 1]  # the version is fed back as `since` to wait for the next change


def test_run_retries_after_a_feed_request_failure() -> None:
    class _FlakyFeed:
        def __init__(self) -> None:
            self.calls = 0

        def list_tasks_versioned(self, *, since: int = 0, wait: float | None = None):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("service blip")
            return [{"id": "t1", "provisioned": False}], 1

    feed = _FlakyFeed()
    provisioner = _FakeProvisioner({"t1": "panopticon/a"})
    slept: list[float] = []
    daemon = ProvisionDaemon(feed, provisioner, sleep=slept.append, interval=2.0)  # type: ignore[arg-type]

    passes = {"n": 0}

    def until() -> bool:  # one failing pass, one good pass, then stop
        done = passes["n"] >= 2
        passes["n"] += 1
        return done

    daemon.run(until=until)
    assert slept == [2.0]  # backed off once after the failure
    assert provisioner.seen == ["t1"]  # the retry provisioned the task


def test_daemon_against_the_real_service(tmp_path: Path) -> None:
    """The loop branches a task the moment the change feed surfaces its slug, then no-ops — end to
    end over REST. `git` is faked; the branch + clone path land on the real task service."""
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

        def fake_run(args: object, *, check: bool = True) -> str:
            return ""

        provisioner = Provisioner(client, clones_root="/clones", git=GitClones(run=fake_run))  # type: ignore[arg-type]
        daemon = ProvisionDaemon(client, provisioner, sleep=lambda _s: None)

        # Pass 1: no slug yet → nothing provisioned.
        tasks, _ = client.list_tasks_versioned()
        assert daemon.provision(tasks) == []
        assert client.get_task(task_id)["branch"] is None

        # The agent sets the slug; the woken snapshot carries it and the pass branches.
        client.set_slug(task_id, "fix-widget")
        tasks, _ = client.list_tasks_versioned()
        assert daemon.provision(tasks) == ["panopticon/fix-widget"]
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == f"/clones/{task_id}"  # the per-task clone path

        # Pass 3: already branched → it's filtered out of the watch set, so it's a no-op.
        tasks, _ = client.list_tasks_versioned()
        assert daemon.provision(tasks) == []


def test_watched_tasks_filters_a_snapshot_to_the_unprovisioned() -> None:
    unprovisioned = {"id": "t1", "provisioned": False}
    done = {"id": "t2", "provisioned": True}
    # the provisioned task drops out of the watch set; only the unprovisioned one remains
    assert watched_tasks([unprovisioned, done]) == [unprovisioned]


def test_run_daemon_provisions_a_slugged_task_over_one_pass(tmp_path: Path) -> None:
    """The launch path (`run_daemon`) wires the provisioner + watch-set and branches a slugged
    task — end to end over REST, `git` faked, stopped after one pass."""
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

        def fake_run(args: object, *, check: bool = True) -> str:
            return ""

        passes = {"n": 0}

        def until() -> bool:  # one pass, then stop
            done = passes["n"] >= 1
            passes["n"] += 1
            return done

        run_daemon(
            client,
            tasks_root="/clones",
            git=GitClones(run=fake_run),
            until=until,
            sleep=lambda _s: None,
        )
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == f"/clones/{task_id}"
