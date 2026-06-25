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
from panopticon.core.models import LifecyclePhase, Repo
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
    """Records spawn calls; stands in for LocalRunner. Mimics its ``progress`` callbacks (STARTING
    then AWAITING), ``is_running`` (for reconcile/down-detection) and ``has_session`` (for heal/
    self-heal) — both configurable."""

    def __init__(self, *, running: bool = True, session: bool = True) -> None:
        self.spawned: list[dict[str, object]] = []
        self._running = running
        self._session = session

    def spawn(self, task_id: str, *, env_file: str | None = None, creds_volume: str | None = None, workspace: str | None = None, image: str | None = None, docker_in_docker: bool = False, memo: str | None = None, progress: Callable[[LifecyclePhase], None] | None = None) -> str:
        self.spawned.append({"task_id": task_id, "env_file": env_file, "creds_volume": creds_volume, "workspace": workspace, "image": image, "docker_in_docker": docker_in_docker, "memo": memo})
        if progress is not None:  # the real runner reports these two sub-steps
            progress(LifecyclePhase.STARTING)
            progress(LifecyclePhase.AWAITING)
        return f"panopticon-{task_id}"

    def is_running(self, task_id: str) -> bool:
        return self._running

    def has_session(self, task_id: str) -> bool:
        return self._session


class _FakeClient:
    """Captures claims + reported lifecycle phases; serves one repo. `claim` 409s when already held
    by another runner."""

    def __init__(self, *, repo: JsonObj, image_layer: str = "", repo_layer: str = "") -> None:
        self._repo = repo
        self._image_layer = image_layer
        self._repo_layer = repo_layer
        self.claims: list[tuple[str, str]] = []
        self._held_by: dict[str, str] = {}
        self.phases: list[tuple[str, str, str | None]] = []  # (task_id, phase, detail)
        self.cleared: list[str] = []

    def workflow_image_layer(self, name: str) -> str:
        return self._image_layer

    def repo_image_layer(self, repo_id: str) -> str:
        return self._repo_layer

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

    def report_lifecycle(self, task_id: str, runner_id: str, phase: str, detail: str | None = None) -> JsonObj:
        self.phases.append((task_id, phase, detail))
        return {"id": task_id}

    def clear_lifecycle(self, task_id: str) -> JsonObj:
        self.cleared.append(task_id)
        return {"id": task_id}


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
    assert runner.spawned[0]["docker_in_docker"] is False  # no capability → unprivileged


def test_spawn_one_passes_the_task_memo_for_input_prefill() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING",
         "claimed_by": None, "memo": "build the thing"}
    )
    # the runner prefills claude's input box with this on a first spawn (left unsent)
    assert runner.spawned[0]["memo"] == "build the thing"


def test_spawn_one_passes_the_docker_in_docker_capability() -> None:
    repo = {**_REPO, "capabilities": {"docker_in_docker": True}}
    client, runner = _FakeClient(repo=repo), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": None}
    )
    assert runner.spawned[0]["docker_in_docker"] is True  # repo opted in → privileged DinD


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
    spawner.spawn_one({"id": "t1", "repo_id": "r1", "workflow": "github-peer-reviewed", "state": "PLANNING", "claimed_by": None})
    assert images.built == [("github-peer-reviewed", "r1", ["RUN apt-get install --yes gh"])]  # composed base → layer
    assert runner.spawned[0]["image"] == "panopticon-github-peer-reviewed-r1"  # spawned on the composed image


def test_spawn_one_composes_workflow_then_repo_layers() -> None:
    # base → workflow (gh) → repo (toolchain), in that order (ADR 0005 tiers). The repo layer is
    # fetched over REST (repo_image_layer), not read inline off the repo record.
    repo = {**_REPO, "image_layer_file": "r1.layer"}
    client = _FakeClient(repo=repo, image_layer="RUN apt-get install --yes gh", repo_layer="RUN pip install uv")
    runner, images = _FakeRunner(), _FakeImageBuilder()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True)  # type: ignore[arg-type]
    spawner = Spawner(
        client, runner, runner_id="host-1", cache=cache, tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run), images=images,  # type: ignore[arg-type]
    )
    spawner.spawn_one({"id": "t1", "repo_id": "r1", "workflow": "github-peer-reviewed", "state": "PLANNING", "claimed_by": None})
    assert images.built == [("github-peer-reviewed", "r1", ["RUN apt-get install --yes gh", "RUN pip install uv"])]
    assert runner.spawned[0]["image"] == "panopticon-github-peer-reviewed-r1"


def test_spawn_one_reports_the_phase_sequence() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
    )
    # claiming → preparing → building (in spawn_one), then starting → awaiting (from the runner)
    assert [p for _, p, _ in client.phases] == ["claiming", "preparing", "building", "starting", "awaiting"]
    assert all(tid == "t1" for tid, _, _ in client.phases)


def test_spawn_one_reports_failed_with_the_error_when_a_step_raises() -> None:
    class _BoomRunner(_FakeRunner):
        def spawn(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("docker run blew up")

    client = _FakeClient(repo=_REPO)
    with pytest.raises(RuntimeError):
        _spawner(client, _BoomRunner()).spawn_one(
            {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
        )
    last_task, last_phase, last_detail = client.phases[-1]
    assert (last_task, last_phase) == ("t1", "failed")
    assert "docker run blew up" in (last_detail or "")  # the failure reason is surfaced


def test_reconcile_clears_a_stale_phase_when_the_container_is_gone() -> None:
    # claimed by us, an in-flight phase, but the container isn't running → clear → composes `down`.
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(running=False)
    _spawner(client, runner).reconcile(
        {"id": "t1", "claimed_by": "host-1", "container_status": "awaiting", "state": "ITERATING"}
    )
    assert client.cleared == ["t1"]


def test_reconcile_leaves_a_still_running_container_alone() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(running=True)
    _spawner(client, runner).reconcile(
        {"id": "t1", "claimed_by": "host-1", "container_status": "awaiting", "state": "ITERATING"}
    )
    assert client.cleared == []  # container present, just not registered yet — keep coming up


def test_reconcile_ignores_tasks_not_in_flight_or_not_ours() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(running=False)
    spawner = _spawner(client, runner)
    spawner.reconcile({"id": "t1", "claimed_by": "host-1", "container_status": "live", "state": "ITERATING"})
    spawner.reconcile({"id": "t2", "claimed_by": "host-1", "container_status": "failed", "state": "ITERATING"})
    spawner.reconcile({"id": "t3", "claimed_by": "host-9", "container_status": "awaiting", "state": "ITERATING"})
    assert client.cleared == []  # live/failed are left as-is; t3 belongs to another runner


def test_heal_respawns_an_orphan_claimed_by_us_with_no_session() -> None:
    # The make-stop case: claimed by us, non-terminal, but its tmux session is gone → respawn it
    # via the idempotent spawn path (the runner docker-rm's the stale container + starts fresh).
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    cid = _spawner(client, runner).heal(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": "host-1"}
    )
    assert cid == "panopticon-t1"
    assert [s["task_id"] for s in runner.spawned] == ["t1"]  # respawned
    assert client.claims == []  # already ours — heal doesn't re-claim


def test_heal_skips_a_task_whose_session_is_alive() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=True)
    assert _spawner(client, runner).heal(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": "host-1"}
    ) is None
    assert runner.spawned == []  # reachable session (e.g. a runner-only restart) — left untouched


def test_heal_skips_tasks_not_claimed_by_this_runner() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = _spawner(client, runner)
    assert spawner.heal({"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": None}) is None
    assert spawner.heal({"id": "t2", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": "host-9"}) is None
    assert runner.spawned == []  # unclaimed → spawn_one's job; another host's → not ours


def test_heal_skips_terminal_tasks() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    assert _spawner(client, runner).heal(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "COMPLETE", "claimed_by": "host-1"}
    ) is None
    assert runner.spawned == []  # done — nothing to keep alive


def _orphan(task_id: str = "t1") -> JsonObj:
    return {"id": task_id, "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": "host-1"}


def test_heal_caps_respawns_then_surfaces_a_crash_looping_task() -> None:
    # A container that won't stay up (session keeps vanishing right away) is respawned only up to the
    # cap, then left for attention — not thrashed forever.
    clock = {"t": 0.0}
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = Spawner(
        client, runner, runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache("/cache", run=_no_op_run, exists=lambda _p: True), tasks_root="/tasks",
        git=GitClones(run=_no_op_run), now=lambda: clock["t"], max_respawns=3, respawn_reset=60.0,
    )
    for _ in range(6):
        spawner.heal(_orphan())
        clock["t"] += 1.0  # rapid failures, well within the reset window
    assert len(runner.spawned) == 3  # capped at max_respawns; further attempts are surfaced, not spawned


def test_heal_resets_the_respawn_budget_after_a_survivor_window() -> None:
    # An isolated orphan that recovers (survives past the reset window) heals again on a later,
    # unrelated failure rather than being counted toward the earlier burst.
    clock = {"t": 0.0}
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = Spawner(
        client, runner, runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache("/cache", run=_no_op_run, exists=lambda _p: True), tasks_root="/tasks",
        git=GitClones(run=_no_op_run), now=lambda: clock["t"], max_respawns=2, respawn_reset=60.0,
    )
    spawner.heal(_orphan())  # respawn 1
    spawner.heal(_orphan())  # respawn 2 → budget now exhausted
    assert len(runner.spawned) == 2
    clock["t"] += 120.0  # the last respawn survived past the reset window
    spawner.heal(_orphan())  # a fresh episode → budget reset → respawns again
    assert len(runner.spawned) == 3


def test_mark_healing_flags_an_orphan_without_respawning_it() -> None:
    # The pre-pass: an orphan (claimed by us, no session) is reported `healing` up front — a
    # REST-only flag, no spawn — so it reads `healing` while it waits behind the serial respawn.
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    _spawner(client, runner).mark_healing(_orphan())
    assert client.phases == [("t1", "healing", None)]
    assert runner.spawned == []  # flagging only — the respawn is heal()'s job


def test_mark_healing_skips_healthy_unclaimed_and_terminal_tasks() -> None:
    client = _FakeClient(repo=_REPO)
    _spawner(client, _FakeRunner(session=True)).mark_healing(_orphan())  # session alive → reachable
    _spawner(client, _FakeRunner(session=False)).mark_healing(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": "host-9"}
    )  # another runner's
    _spawner(client, _FakeRunner(session=False)).mark_healing(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "COMPLETE", "claimed_by": "host-1"}
    )  # terminal
    assert client.phases == []  # nothing to heal → nothing flagged


def test_mark_healing_does_not_re_flag_an_already_healing_orphan() -> None:
    # Idempotent across passes: once an orphan reads `healing`, re-flagging it would only churn the
    # change feed (waking the dashboard for no change), so it's skipped.
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    orphan = _orphan()
    orphan["container_status"] = "healing"  # the prior pass already flagged it
    _spawner(client, runner).mark_healing(orphan)
    assert client.phases == []


def test_mark_healing_skips_a_crash_looped_out_orphan() -> None:
    # Once we've stopped respawning a task (budget exhausted), it should read `down`/`failed`, not a
    # perpetual `healing` — so the pre-pass stops flagging it too.
    clock = {"t": 0.0}
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = Spawner(
        client, runner, runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache("/cache", run=_no_op_run, exists=lambda _p: True), tasks_root="/tasks",
        git=GitClones(run=_no_op_run), now=lambda: clock["t"], max_respawns=2, respawn_reset=60.0,
    )
    spawner.heal(_orphan()); spawner.heal(_orphan())  # exhaust the respawn budget
    client.phases.clear()
    spawner.mark_healing(_orphan())  # capped out → not flagged
    assert client.phases == []


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
