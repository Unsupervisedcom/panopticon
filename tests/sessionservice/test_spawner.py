"""Host-side spawn loop (ADR 0008): claim an unclaimed task, then spawn its container. Unit tests
drive `spawn_one`/`spawnable_tasks` with fakes; an integration test runs against the real task
service over REST. No Docker, no LLM — `git`/runner are fakes."""

from __future__ import annotations

import asyncio
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

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        workspace: str | None = None,
        image: str | None = None,
        docker_in_docker: bool = False,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        self.spawned.append(
            {
                "task_id": task_id,
                "env_file": env_file,
                "workspace": workspace,
                "image": image,
                "docker_in_docker": docker_in_docker,
                "initial_prompt": initial_prompt,
                "turn": turn,
                "starting_model": starting_model,
            }
        )
        if progress is not None:  # the real runner reports these two sub-steps
            progress(LifecyclePhase.STARTING)
            progress(LifecyclePhase.AWAITING)
        return f"panopticon-{task_id}"

    def is_running(self, task_id: str) -> bool:
        return self._running

    def has_session(self, task_id: str) -> bool:
        return self._session

    def stop(self, container_id: str) -> None:
        pass

    def delete_workspace_contents(self, path: str) -> None:
        pass


class _FakeClient:
    """Captures claims + reported lifecycle phases; serves one repo. `claim` 409s when already held
    by another runner."""

    def __init__(
        self,
        *,
        repo: JsonObj,
        image_layer: str = "",
        repo_layer: str = "",
        runner_type: str = "docker",
        shell_script: str = "",
        clone_repo: bool = False,
        shell_workdir: str | None = None,
    ) -> None:
        self._repo = repo
        self._image_layer = image_layer
        self._repo_layer = repo_layer
        self._runner_type = runner_type
        self._shell_spec = {
            "script": shell_script,
            "clone_repo": clone_repo,
            "workdir": shell_workdir,
        }
        self.claims: list[tuple[str, str]] = []
        self._held_by: dict[str, str] = {}
        self.phases: list[tuple[str, str, str | None]] = []  # (task_id, phase, detail)
        self.cleared: list[str] = []
        self.releases: list[str] = []

    def workflow_image_layer(self, name: str) -> str:
        return self._image_layer

    def repo_image_layer(self, repo_id: str) -> str:
        return self._repo_layer

    def workflow_execution(self, name: str) -> JsonObj:
        return {"runner_type": self._runner_type, **self._shell_spec}

    def claim(self, task_id: str, runner_id: str) -> JsonObj:
        holder = self._held_by.get(task_id)
        if holder not in (None, runner_id):
            request = httpx.Request("PUT", f"http://svc/tasks/{task_id}/claim")
            raise httpx.HTTPStatusError(
                "conflict", request=request, response=httpx.Response(409, request=request)
            )
        self._held_by[task_id] = runner_id
        self.claims.append((task_id, runner_id))
        return {"id": task_id, "claimed_by": runner_id}

    def hold(self, task_id: str, runner_id: str) -> None:
        self._held_by[task_id] = runner_id  # simulate another runner already holding it

    def get_repo(self, repo_id: str) -> JsonObj:
        return self._repo

    def report_lifecycle(
        self, task_id: str, runner_id: str, phase: str, detail: str | None = None
    ) -> JsonObj:
        self.phases.append((task_id, phase, detail))
        return {"id": task_id}

    def clear_lifecycle(self, task_id: str) -> JsonObj:
        self.cleared.append(task_id)
        return {"id": task_id}

    def release(self, task_id: str) -> JsonObj:
        self.releases.append(task_id)
        return {"id": task_id}


def _spawner(client: object, runner: object, images: object = None) -> Spawner:
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    return Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=images or _FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
    )


_REPO: JsonObj = {"id": "r1", "git_url": "https://forge/r1.git", "env_file": "r1.env"}


def test_spawn_one_claims_then_spawns_a_fresh_task() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    cid = _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": None}
    )
    assert cid == "panopticon-t1"
    assert client.claims == [("t1", "host-1")]  # claimed for this host first
    assert runner.spawned[0]["workspace"] == "/tasks/t1"  # per-task clone mounted
    assert runner.spawned[0]["env_file"] == "r1.env"
    assert runner.spawned[0]["image"] is None  # spike has no image layer → runner uses the base
    assert runner.spawned[0]["docker_in_docker"] is False  # no capability → unprivileged


def test_spawn_one_passes_initial_prompt_as_env_var() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "PLANNING",
            "claimed_by": None,
            "memo": "build the thing",
            "initial_prompt": "review your plan",
        }
    )
    assert runner.spawned[0]["initial_prompt"] == "review your plan"


def test_spawn_one_passes_turn_for_interrupt_prompt_on_respawn() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": None,
            "turn": "agent",
        }
    )
    assert runner.spawned[0]["turn"] == "agent"


def test_spawn_one_passes_starting_model_to_runner() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": None,
            "starting_model": "opus",
        }
    )
    assert runner.spawned[0]["starting_model"] == "opus"


def test_spawn_one_passes_the_docker_in_docker_capability() -> None:
    repo = {**_REPO, "capabilities": {"docker_in_docker": True}}
    client, runner = _FakeClient(repo=repo), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "ITERATING", "claimed_by": None}
    )
    assert runner.spawned[0]["docker_in_docker"] is True  # repo opted in → privileged DinD


class _FakeShellRunner:
    """Records shell-session spawns; stands in for ShellRunner. Mimics its ``progress`` callbacks
    (STARTING then AWAITING) and ``is_running``/``has_session`` (the session == liveness)."""

    def __init__(self, *, session: bool = True) -> None:
        self.spawned: list[dict[str, object]] = []
        self._session = session

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        git_url: str | None = None,
        repo_name: str | None = None,
        script: str = "",
        workdir: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        self.spawned.append(
            {
                "task_id": task_id,
                "env_file": env_file,
                "git_url": git_url,
                "repo_name": repo_name,
                "script": script,
                "workdir": workdir,
            }
        )
        if progress is not None:
            progress(LifecyclePhase.STARTING)
            progress(LifecyclePhase.AWAITING)
        return f"panopticon-{task_id}"

    def is_running(self, task_id: str) -> bool:
        return self._session

    def has_session(self, task_id: str) -> bool:
        return self._session

    def stop(self, session_id: str) -> None:
        pass


def _shell_spawner(
    client: object, runner: object, shell_runner: object, made: list[str] | None = None
) -> Spawner:
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    return Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        shell_runner=shell_runner,
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=(made.append if made is not None else (lambda _p: None)),
    )


_SHELL_TASK: JsonObj = {
    "id": "t1",
    "repo_id": "r1",
    "workflow": "setup-repo",
    "state": "RUNNING",
    "claimed_by": None,
}


def test_spawn_one_shell_workflow_runs_the_script_and_skips_docker() -> None:
    repo = {**_REPO, "name": "acme/widget"}
    client = _FakeClient(repo=repo, runner_type="shell", shell_script="claude setup-token")
    runner, shell = _FakeRunner(), _FakeShellRunner()
    made: list[str] = []
    cid = _shell_spawner(client, runner, shell, made).spawn_one(dict(_SHELL_TASK))
    assert cid == "panopticon-t1"
    assert runner.spawned == []  # the Docker runner is never touched for a shell workflow
    assert shell.spawned[0]["task_id"] == "t1"
    assert shell.spawned[0]["script"] == "claude setup-token"  # fetched from the workflow over REST
    assert shell.spawned[0]["env_file"] == "r1.env"  # the repo's secrets name is passed through
    assert shell.spawned[0]["git_url"] == "https://forge/r1.git"  # the repo's forge, for detection
    assert shell.spawned[0]["repo_name"] == "acme/widget"  # the repo's label, for the summary
    # by default the script runs in the task's own directory, created empty (no clone)
    assert shell.spawned[0]["workdir"] == "/tasks/t1"
    assert made == ["/tasks/t1"]


def test_spawn_one_shell_reports_phases_without_building() -> None:
    # No image build → BUILDING falls away; PREPARING stays (the task dir is still readied).
    client = _FakeClient(repo=_REPO, runner_type="shell", shell_script="echo hi")
    _shell_spawner(client, _FakeRunner(), _FakeShellRunner()).spawn_one(dict(_SHELL_TASK))
    assert [p for _, p, _ in client.phases] == ["claiming", "preparing", "starting", "awaiting"]


def test_spawn_one_shell_workflow_clones_the_repo_when_opted_in() -> None:
    # clone_repo=True → the task dir is a real per-task clone (prepare_workspace), like a container.
    client = _FakeClient(
        repo=_REPO, runner_type="shell", shell_script="make build", clone_repo=True
    )
    runner, shell = _FakeRunner(), _FakeShellRunner()
    made: list[str] = []
    _shell_spawner(client, runner, shell, made).spawn_one(dict(_SHELL_TASK))
    assert shell.spawned[0]["workdir"] == "/tasks/t1"  # the clone dir is still the task dir
    # prepare_workspace (the clone path) makes the *parent* dir; the bare no-clone path would
    # instead makedirs the task dir itself ("/tasks/t1") — so this proves the repo was cloned.
    assert made == ["/tasks"]


def test_spawn_one_shell_workflow_honours_a_workdir_override() -> None:
    # A workflow can start its script somewhere other than the task dir (e.g. the operator's home).
    client = _FakeClient(
        repo=_REPO, runner_type="shell", shell_script="echo hi", shell_workdir="/home/op"
    )
    runner, shell = _FakeRunner(), _FakeShellRunner()
    _shell_spawner(client, runner, shell).spawn_one(dict(_SHELL_TASK))
    assert shell.spawned[0]["workdir"] == "/home/op"  # the override wins over the task dir


def test_spawn_shell_workflow_without_a_shell_runner_fails() -> None:
    # A shell workflow on a host with no shell runner is a misconfiguration, surfaced as FAILED.
    client = _FakeClient(repo=_REPO, runner_type="shell", shell_script="echo hi")
    with pytest.raises(RuntimeError, match="no shell runner"):
        _spawner(client, _FakeRunner()).spawn_one(dict(_SHELL_TASK))
    assert any(p == "failed" for _, p, _ in client.phases)


def test_heal_never_respawns_a_shell_task() -> None:
    # A shell script exiting is natural completion (or an operator cancelling), not a crash — so an
    # orphaned shell task (claimed by us, session gone) is left alone, never respawned.
    client = _FakeClient(repo=_REPO, runner_type="shell", shell_script="echo hi")
    runner, shell = _FakeRunner(session=False), _FakeShellRunner(session=False)
    assert (
        _shell_spawner(client, runner, shell).heal(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "setup-repo",
                "state": "RUNNING",
                "claimed_by": "host-1",
            }
        )
        is None
    )
    assert shell.spawned == [] and runner.spawned == []


def test_startup_reclaim_keeps_a_shell_task_claimed_so_it_is_not_re_run() -> None:
    # Releasing a shell task's claim would let spawn_one re-run its script (the unclaimed-spawn path);
    # a shell script runs once, so a session-gone shell task stays claimed (reconciles to `down`).
    client = _FakeClient(repo=_REPO, runner_type="shell", shell_script="echo hi")
    runner, shell = _FakeRunner(running=False, session=False), _FakeShellRunner(session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "setup-repo",
            "state": "RUNNING",
            "claimed_by": "host-1",
        }
    ]
    _shell_spawner(client, runner, shell).startup_reclaim(tasks)
    assert client.releases == []  # not released → spawn_one won't re-run it


def test_reconcile_probes_the_shell_session_not_docker() -> None:
    # A running shell task (session alive) must not be reconciled to `down` just because there's no
    # Docker container — reconcile checks the shell runner's session for a shell workflow.
    client = _FakeClient(repo=_REPO, runner_type="shell")
    runner, shell = _FakeRunner(running=False), _FakeShellRunner(session=True)
    _shell_spawner(client, runner, shell).reconcile(
        {
            "id": "t1",
            "workflow": "setup-repo",
            "claimed_by": "host-1",
            "container_status": "awaiting",
            "state": "RUNNING",
        }
    )
    assert client.cleared == []  # session up → left alone (would be cleared if it probed Docker)


class _FakeImageBuilder:
    """Records compose calls; stands in for ImageBuilder (no docker)."""

    def __init__(self) -> None:
        self.built: list[tuple[str, str, list[str]]] = []
        self.base_checks: int = 0

    def build(
        self, workflow: str, repo_id: str, layers: list[str], *, verbose: bool = False
    ) -> str:
        self.built.append((workflow, repo_id, layers))
        return f"panopticon-{workflow}-{repo_id}"

    def build_base_if_missing(self, *, verbose: bool = False) -> bool:
        self.base_checks += 1
        return False  # image is always "present" in tests — no build triggered


def test_spawn_one_composes_the_workflow_image_when_it_has_a_layer() -> None:
    client, runner = (
        _FakeClient(repo=_REPO, image_layer="RUN apt-get install --yes gh"),
        _FakeRunner(),
    )
    images = _FakeImageBuilder()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=images,
        makedirs=lambda _p: None,  # type: ignore[arg-type]
    )
    spawner.spawn_one(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "github-peer-reviewed",
            "state": "PLANNING",
            "claimed_by": None,
        }
    )
    assert images.built == [
        ("github-peer-reviewed", "r1", ["RUN apt-get install --yes gh"])
    ]  # composed base → layer
    assert (
        runner.spawned[0]["image"] == "panopticon-github-peer-reviewed-r1"
    )  # spawned on the composed image


def test_spawn_one_composes_workflow_then_repo_layers() -> None:
    # base → workflow (gh) → repo (toolchain), in that order (ADR 0005 tiers). The repo layer is
    # fetched over REST (repo_image_layer), not read inline off the repo record.
    repo = {**_REPO, "image_layer_file": "r1.layer"}
    client = _FakeClient(
        repo=repo, image_layer="RUN apt-get install --yes gh", repo_layer="RUN pip install uv"
    )
    runner, images = _FakeRunner(), _FakeImageBuilder()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=images,
        makedirs=lambda _p: None,  # type: ignore[arg-type]
    )
    spawner.spawn_one(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "github-peer-reviewed",
            "state": "PLANNING",
            "claimed_by": None,
        }
    )
    assert images.built == [
        ("github-peer-reviewed", "r1", ["RUN apt-get install --yes gh", "RUN pip install uv"])
    ]
    assert runner.spawned[0]["image"] == "panopticon-github-peer-reviewed-r1"


def test_spawn_one_probes_base_image_during_building_phase() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    images = _FakeImageBuilder()
    _spawner(client, runner, images=images).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
    )
    assert images.base_checks == 1  # probed exactly once per spawn


def test_spawn_one_reports_the_phase_sequence() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    _spawner(client, runner).spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
    )
    # claiming → preparing → building (in spawn_one), then starting → awaiting (from the runner)
    assert [p for _, p, _ in client.phases] == [
        "claiming",
        "preparing",
        "building",
        "starting",
        "awaiting",
    ]
    assert all(tid == "t1" for tid, _, _ in client.phases)


def test_spawn_one_reports_failed_with_the_error_when_a_step_raises() -> None:
    class _BoomRunner(_FakeRunner):
        def spawn(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("docker run blew up")

    client = _FakeClient(repo=_REPO)
    with pytest.raises(RuntimeError):
        _spawner(client, _BoomRunner()).spawn_one(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "PLANNING",
                "claimed_by": None,
            }
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
    spawner.reconcile(
        {"id": "t1", "claimed_by": "host-1", "container_status": "live", "state": "ITERATING"}
    )
    spawner.reconcile(
        {"id": "t2", "claimed_by": "host-1", "container_status": "failed", "state": "ITERATING"}
    )
    spawner.reconcile(
        {"id": "t3", "claimed_by": "host-9", "container_status": "awaiting", "state": "ITERATING"}
    )
    assert client.cleared == []  # live/failed are left as-is; t3 belongs to another runner


def test_heal_respawns_an_orphan_claimed_by_us_with_no_session() -> None:
    # The orphan case (e.g. the tmux server crashed, or `make stop` tore everything down but the
    # task stays claimed): claimed by us, non-terminal, but its tmux session is gone → respawn it
    # via the idempotent spawn path (the runner docker-rm's any stale container + starts fresh).
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    cid = _spawner(client, runner).heal(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-1",
        }
    )
    assert cid == "panopticon-t1"
    assert [s["task_id"] for s in runner.spawned] == ["t1"]  # respawned
    assert client.claims == []  # already ours — heal doesn't re-claim


def test_heal_skips_a_task_whose_session_is_alive() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=True)
    assert (
        _spawner(client, runner).heal(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "ITERATING",
                "claimed_by": "host-1",
            }
        )
        is None
    )
    assert runner.spawned == []  # reachable session (e.g. a runner-only restart) — left untouched


def test_heal_skips_tasks_not_claimed_by_this_runner() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = _spawner(client, runner)
    assert (
        spawner.heal(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "ITERATING",
                "claimed_by": None,
            }
        )
        is None
    )
    assert (
        spawner.heal(
            {
                "id": "t2",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "ITERATING",
                "claimed_by": "host-9",
            }
        )
        is None
    )
    assert runner.spawned == []  # unclaimed → spawn_one's job; another host's → not ours


def test_heal_skips_terminal_tasks() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    assert (
        _spawner(client, runner).heal(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "COMPLETE",
                "claimed_by": "host-1",
            }
        )
        is None
    )
    assert runner.spawned == []  # done — nothing to keep alive


def _orphan(task_id: str = "t1") -> JsonObj:
    return {
        "id": task_id,
        "repo_id": "r1",
        "workflow": "spike",
        "state": "ITERATING",
        "claimed_by": "host-1",
    }


def test_heal_caps_respawns_then_surfaces_a_crash_looping_task() -> None:
    # A container that won't stay up (session keeps vanishing right away) is respawned only up to the
    # cap, then left for attention — not thrashed forever.
    clock = {"t": 0.0}
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache(
            "/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None
        ),
        tasks_root="/tasks",
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        now=lambda: clock["t"],
        max_respawns=3,
        respawn_reset=60.0,
    )
    for _ in range(6):
        spawner.heal(_orphan())
        clock["t"] += 1.0  # rapid failures, well within the reset window
    assert (
        len(runner.spawned) == 3
    )  # capped at max_respawns; further attempts are surfaced, not spawned


def test_heal_resets_the_respawn_budget_after_a_survivor_window() -> None:
    # An isolated orphan that recovers (survives past the reset window) heals again on a later,
    # unrelated failure rather than being counted toward the earlier burst.
    clock = {"t": 0.0}
    client, runner = _FakeClient(repo=_REPO), _FakeRunner(session=False)
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache(
            "/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None
        ),
        tasks_root="/tasks",
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        now=lambda: clock["t"],
        max_respawns=2,
        respawn_reset=60.0,
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
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-9",
        }
    )  # another runner's
    _spawner(client, _FakeRunner(session=False)).mark_healing(
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "COMPLETE",
            "claimed_by": "host-1",
        }
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
        client,
        runner,
        runner_id="host-1",  # type: ignore[arg-type]
        cache=CloneCache(
            "/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None
        ),
        tasks_root="/tasks",
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        now=lambda: clock["t"],
        max_respawns=2,
        respawn_reset=60.0,
    )
    spawner.heal(_orphan())  # exhaust the respawn budget
    spawner.heal(_orphan())
    client.phases.clear()
    spawner.mark_healing(_orphan())  # capped out → not flagged
    assert client.phases == []


def test_startup_reclaim_releases_our_claimed_tasks_whose_containers_are_gone() -> None:
    # After a clean stop or reboot, containers are gone — release claims so tasks restart
    # from `queued` and go through the full visible spawn lifecycle.
    client = _FakeClient(repo=_REPO)
    runner = _FakeRunner(running=False, session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-1",
        },
        {
            "id": "t2",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-1",
        },
    ]
    _spawner(client, runner).startup_reclaim(tasks)
    assert client.releases == ["t1", "t2"]


def test_startup_reclaim_keeps_claims_when_container_is_still_running() -> None:
    # Runner-only crash: the container survived → keep the claim so heal() resumes it.
    client = _FakeClient(repo=_REPO)
    runner = _FakeRunner(running=True, session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-1",
        }
    ]
    _spawner(client, runner).startup_reclaim(tasks)
    assert client.releases == []


def test_startup_reclaim_skips_tasks_not_claimed_by_us() -> None:
    client = _FakeClient(repo=_REPO)
    runner = _FakeRunner(running=False, session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": None,
        },
        {
            "id": "t2",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-9",
        },
    ]
    _spawner(client, runner).startup_reclaim(tasks)
    assert client.releases == []


def test_startup_reclaim_skips_terminal_tasks() -> None:
    client = _FakeClient(repo=_REPO)
    runner = _FakeRunner(running=False, session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "COMPLETE",
            "claimed_by": "host-1",
        }
    ]
    _spawner(client, runner).startup_reclaim(tasks)
    assert client.releases == []


def test_startup_reclaim_ignores_release_errors() -> None:
    # Best-effort: a failed release is silently skipped — heal() will pick it up.
    class _ErrorClient(_FakeClient):
        def release(self, task_id: str) -> JsonObj:
            request = httpx.Request("DELETE", f"http://svc/tasks/{task_id}/claim")
            raise httpx.HTTPStatusError(
                "gone", request=request, response=httpx.Response(500, request=request)
            )

    client = _ErrorClient(repo=_REPO)
    runner = _FakeRunner(running=False, session=False)
    tasks = [
        {
            "id": "t1",
            "repo_id": "r1",
            "workflow": "spike",
            "state": "ITERATING",
            "claimed_by": "host-1",
        }
    ]
    _spawner(client, runner).startup_reclaim(tasks)  # must not raise


def test_spawn_one_skips_terminal_and_already_claimed_tasks() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    spawner = _spawner(client, runner)
    assert (
        spawner.spawn_one({"id": "t1", "repo_id": "r1", "state": "COMPLETE", "claimed_by": None})
        is None
    )
    assert (
        spawner.spawn_one(
            {"id": "t2", "repo_id": "r1", "state": "ITERATING", "claimed_by": "host-9"}
        )
        is None
    )
    assert client.claims == [] and runner.spawned == []


def test_spawn_one_skips_when_another_runner_wins_the_claim() -> None:
    client, runner = _FakeClient(repo=_REPO), _FakeRunner()
    client.hold("t1", "host-2")  # another runner grabbed it between our snapshot and claim
    assert (
        _spawner(client, runner).spawn_one(
            {"id": "t1", "repo_id": "r1", "state": "ITERATING", "claimed_by": None}
        )
        is None
    )
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


def test_spawn_runs_repo_hook_with_correct_args() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def _fake_hook(hook_file: str, task_id: str, repo_name: str, workspace: str) -> None:
        calls.append((hook_file, task_id, repo_name, workspace))

    repo = {**_REPO, "name": "acme/widgets", "hook_file": "/hooks/acme.sh"}
    client, runner = _FakeClient(repo=repo), _FakeRunner()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        run_hook=_fake_hook,
        makedirs=lambda _p: None,
    )
    spawner.spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
    )
    assert calls == [("/hooks/acme.sh", "t1", "acme/widgets", "/tasks/t1")]
    assert runner.spawned  # container still spawned after the hook


def _cleanup_spawner(runner: _FakeRunner, *, workspace_exists: bool) -> Spawner:
    """Helper: a spawner with injectable exists/rmtree for cleanup tests."""
    client = _FakeClient(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    return Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: workspace_exists,
        rmtree=lambda _p: None,  # swapped out per test when we need to record calls
    )


def test_cleanup_removes_workspace_when_terminal_and_container_gone() -> None:
    removed: list[str] = []
    runner = _FakeRunner(running=False)
    client = _FakeClient(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: True,
        rmtree=removed.append,
    )
    spawner.cleanup({"id": "t1", "state": "COMPLETE"})
    assert removed == ["/tasks/t1"]


def test_cleanup_waits_when_container_still_running() -> None:
    removed: list[str] = []
    runner = _FakeRunner(running=True)
    client = _FakeClient(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: True,
        rmtree=removed.append,
    )
    spawner.cleanup({"id": "t1", "state": "COMPLETE"})
    assert removed == []  # container still up — leave workspace alone


def test_cleanup_is_a_no_op_for_non_terminal_task() -> None:
    removed: list[str] = []
    runner = _FakeRunner(running=False)
    client = _FakeClient(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: True,
        rmtree=removed.append,
    )
    spawner.cleanup({"id": "t1", "state": "ITERATING"})
    assert removed == []  # live task — never touch its workspace


def test_cleanup_invokes_docker_cleanup_when_rmtree_fails() -> None:
    # Spawner wires docker_cleanup through to cleanup_workspace; verify the path end-to-end.
    docker_called: list[str] = []
    rmtree_calls = 0

    def rmtree_first_fails(path: str) -> None:
        nonlocal rmtree_calls
        rmtree_calls += 1
        if rmtree_calls == 1:
            raise PermissionError(13, "Permission denied", "/tasks/t1/.mypy_cache")

    runner = _FakeRunner(running=False)
    client = _FakeClient(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: True,
        rmtree=rmtree_first_fails,
        docker_cleanup=docker_called.append,
    )
    spawner.cleanup({"id": "t1", "state": "COMPLETE"})
    assert docker_called == ["/tasks/t1"]
    assert rmtree_calls == 2


def test_cleanup_unknown_workflow_releases_claim_and_cleans_workspace() -> None:
    """A terminal claimed task whose workflow name is no longer in the registry must not poison
    the host tick. cleanup() should release the claim and remove the workspace without raising,
    because WorkflowExecutions falls back to docker for unknown workflows (4xx)."""
    removed: list[str] = []
    releases: list[str] = []

    class _ClientWith400Execution(_FakeClient):
        def workflow_execution(self, name: str) -> JsonObj:
            request = httpx.Request("GET", f"http://svc/workflows/{name}/execution")
            raise httpx.HTTPStatusError(
                "unknown workflow",
                request=request,
                response=httpx.Response(400, request=request),
            )

        def release(self, task_id: str) -> JsonObj:
            releases.append(task_id)
            return {"id": task_id}

    runner = _FakeRunner(running=False)
    client = _ClientWith400Execution(repo=_REPO)
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        makedirs=lambda _p: None,
        exists=lambda _p: True,
        rmtree=removed.append,
    )
    task = {"id": "t1", "workflow": "parity", "state": "COMPLETE", "claimed_by": "host-1"}
    spawner.cleanup(task)  # must not raise
    assert releases == ["t1"]  # claim drained
    assert removed == ["/tasks/t1"]  # workspace cleaned


def test_spawn_hook_failure_aborts_spawn() -> None:
    def _boom(hook_file: str, task_id: str, repo_name: str, workspace: str) -> None:
        raise RuntimeError("hook exited 1")

    repo = {**_REPO, "name": "acme/widgets", "hook_file": "/hooks/acme.sh"}
    client, runner = _FakeClient(repo=repo), _FakeRunner()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        run_hook=_boom,
        makedirs=lambda _p: None,
    )
    with pytest.raises(RuntimeError, match="hook exited 1"):
        spawner.spawn_one(
            {
                "id": "t1",
                "repo_id": "r1",
                "workflow": "spike",
                "state": "PLANNING",
                "claimed_by": None,
            }
        )
    assert not runner.spawned  # docker run was never called
    assert any(p == "failed" for _, p, _ in client.phases)  # reported as FAILED


def test_spawn_skips_hook_when_repo_has_no_hook_file() -> None:
    calls: list[object] = []
    repo = {**_REPO, "name": "acme/widgets"}  # no hook_file key
    client, runner = _FakeClient(repo=repo), _FakeRunner()
    cache = CloneCache("/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None)  # type: ignore[arg-type]
    spawner = Spawner(
        client,
        runner,
        runner_id="host-1",
        cache=cache,
        tasks_root="/tasks",  # type: ignore[arg-type]
        git=GitClones(run=_no_op_run),
        images=_FakeImageBuilder(),  # type: ignore[arg-type]
        run_hook=lambda *a: calls.append(a),
        makedirs=lambda _p: None,
    )
    spawner.spawn_one(
        {"id": "t1", "repo_id": "r1", "workflow": "spike", "state": "PLANNING", "claimed_by": None}
    )
    assert not calls  # hook never invoked
    assert runner.spawned  # container spawned normally


def test_spawner_against_the_real_service(tmp_path: Path) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://forge/r1.git"))
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]
        runner = _FakeRunner()
        spawner = Spawner(
            client,
            runner,
            runner_id="host-1",  # type: ignore[arg-type]
            cache=CloneCache(
                "/cache", run=_no_op_run, exists=lambda _p: True, makedirs=lambda _p: None
            ),
            tasks_root="/tasks",
            git=GitClones(run=_no_op_run),
            images=_FakeImageBuilder(),  # type: ignore[arg-type]
            makedirs=lambda _p: None,
        )
        (task,) = spawnable_tasks(client)()  # the fresh task is spawnable
        assert spawner.spawn_one(task) == f"panopticon-{task_id}"
        assert client.get_task(task_id)["claimed_by"] == "host-1"  # claim recorded on the service
        assert spawnable_tasks(client)() == []  # now claimed → no longer spawnable
