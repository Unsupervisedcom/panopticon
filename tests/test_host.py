"""The unified per-host session service daemon (ADR 0008/0011): each pass spawns new tasks and
provisions slugged ones. A unit test isolates per-task errors; an integration test drives the full
spawn→slug→provision flow against the real task service over REST. No Docker, no LLM."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.core.models import Repo
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.host import HostDaemon, run_host
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _no_op_run(args: object, *, check: bool = True) -> str:
    return ""


class _FakeRunner:
    def __init__(self) -> None:
        self.spawned: list[str] = []

    def spawn(self, task_id: str, *, env_file: str | None = None, creds_volume: str | None = None, workspace: str | None = None) -> str:
        self.spawned.append(task_id)
        return f"panopticon-{task_id}"


def test_tick_isolates_a_failing_task_from_the_others() -> None:
    seen: list[str] = []

    class _Spawner:
        def spawn_one(self, task: JsonObj) -> None:
            seen.append(task["id"])
            if task["id"] == "t1":
                raise RuntimeError("boom")

    class _Provisioner:
        def provision(self, task: JsonObj) -> None:
            return None

    class _Client:
        def list_tasks(self) -> list[JsonObj]:
            return [{"id": "t1"}, {"id": "t2"}]

    daemon = HostDaemon(_Client(), _Spawner(), _Provisioner())  # type: ignore[arg-type]
    daemon.tick()
    assert seen == ["t1", "t2"]  # t1's error is logged + skipped; t2 still processed


def test_run_host_spawns_then_provisions_end_to_end(tmp_path: Path) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git", default_base="trunk"))
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        runner = _FakeRunner()

        def one_pass() -> Callable[[], bool]:
            calls = {"n": 0}

            def until() -> bool:
                done = calls["n"] >= 1
                calls["n"] += 1
                return done

            return until

        kw = dict(  # noqa: C408 - readability
            runner_id="host-1", tasks_root="/clones",
            cache=CloneCache("/cache", run=_no_op_run, exists=lambda _p: True),
            git=GitClones(run=_no_op_run), sleep=lambda _s: None,
        )

        # Pass 1: fresh task → claimed + spawned; no slug yet → not provisioned.
        run_host(client, runner, until=one_pass(), **kw)  # type: ignore[arg-type]
        got = client.get_task(task_id)
        assert runner.spawned == [task_id]
        assert got["claimed_by"] == "host-1" and got["branch"] is None

        # The agent sets its slug; next pass → provisioned (branched), no re-spawn (already claimed).
        client.set_slug(task_id, "fix-widget")
        run_host(client, runner, until=one_pass(), **kw)  # type: ignore[arg-type]
        got = client.get_task(task_id)
        assert runner.spawned == [task_id]  # not spawned again
        assert got["branch"] == "panopticon/fix-widget" and got["clone"] == f"/clones/{task_id}"
