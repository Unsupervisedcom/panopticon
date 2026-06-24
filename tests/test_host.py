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

    def spawn(self, task_id: str, *, env_file: str | None = None, creds_volume: str | None = None, workspace: str | None = None, image: str | None = None, docker_in_docker: bool = False, memo: str | None = None) -> str:
        self.spawned.append(task_id)
        return f"panopticon-{task_id}"


class _FakeClient:
    """A do-nothing change-feed client — `tick` takes the snapshot as an argument, so the client is
    irrelevant to the tick-level tests; the constructor just needs *something* shaped like one."""

    def __init__(self, tasks: list[JsonObj]) -> None:
        self._tasks = tasks

    def list_tasks_versioned(self, *, since: int = 0, wait: float | None = None) -> tuple[list[JsonObj], int]:
        return self._tasks, since


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

    daemon = HostDaemon(_FakeClient([]), _Spawner(), _Provisioner())  # type: ignore[arg-type]
    daemon.tick([{"id": "t1"}, {"id": "t2"}])
    assert seen == ["t1", "t2"]  # t1's error is logged + skipped; t2 still processed


def test_run_blocks_on_the_change_feed_and_feeds_the_version_back() -> None:
    # The loop waits on `list_tasks_versioned(wait=, since=)`, not a fixed-interval re-poll: each
    # call's returned version is fed back as the next `since`, so we wake on the *next* change.
    sinces: list[int] = []

    class _Spawner:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def spawn_one(self, task: JsonObj) -> None:
            self.seen.append(task["id"])

    class _Provisioner:
        def provision(self, task: JsonObj) -> None:
            return None

    class _FeedClient:
        def list_tasks_versioned(self, *, since: int = 0, wait: float | None = None) -> tuple[list[JsonObj], int]:
            sinces.append(since)
            return [{"id": f"t{len(sinces)}"}], len(sinces)  # a fresh snapshot + a bumped version

    spawner = _Spawner()
    daemon = HostDaemon(_FeedClient(), spawner, _Provisioner())  # type: ignore[arg-type]
    daemon.run(until=lambda: len(sinces) >= 3)
    assert sinces == [0, 1, 2]  # starts at 0, then each returned version becomes the next `since`
    assert spawner.seen == ["t1", "t2", "t3"]  # ticked the snapshot returned by each wake


def test_run_survives_a_whole_pass_failure() -> None:
    # The blocking call raising (a service blip, or the service not yet listening at startup) must
    # not kill the daemon: the pass is logged + retried after a short sleep, so the loop keeps going.
    passes = {"n": 0}

    class _Spawner:
        def spawn_one(self, task: JsonObj) -> None:
            return None

    class _Provisioner:
        def provision(self, task: JsonObj) -> None:
            return None

    class _FlakyClient:
        def list_tasks_versioned(self, *, since: int = 0, wait: float | None = None) -> tuple[list[JsonObj], int]:
            passes["n"] += 1
            if passes["n"] == 1:
                raise RuntimeError("connection refused")  # first call fails (startup race)
            return [], since

    def until() -> bool:
        return passes["n"] >= 3  # let it wake a few times after the failure

    daemon = HostDaemon(_FlakyClient(), _Spawner(), _Provisioner(), sleep=lambda _s: None)  # type: ignore[arg-type]
    daemon.run(until=until)
    assert passes["n"] >= 3  # did not die on the first pass's error; kept going


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
