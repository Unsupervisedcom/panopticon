"""LocalRunner: unit tests pin the emitted docker/tmux commands; one integration test
exercises a real container + tmux session (skipped when docker/tmux are unavailable)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.runner import Runner


class _Recorder:
    """An injectable CommandRunner that records calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.interactive: list[bool] = []

    def __call__(self, args: Sequence[str], *, check: bool = True, interactive: bool = False, verbose: bool = False) -> str:
        self.calls.append((list(args), check))
        self.interactive.append(interactive)
        return ""


def test_local_runner_is_a_runner() -> None:
    assert issubclass(LocalRunner, Runner)


def test_spawn_runs_detached_container_then_tmux_pane_execing_in() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc:8000", image="img:1", runner_id="r1", run=rec)

    container_id = runner.spawn("t1")

    assert container_id == "panopticon-t1"
    (kill_session, _), (rm, _), (docker_run, _), (tmux_new, _) = rec.calls
    # clear any stale tmux session first (idempotent — no-op when nothing exists)
    assert kill_session == ["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]
    assert rm == ["docker", "rm", "--force", "panopticon-t1"]  # then clear a stale container
    assert docker_run[:3] == ["docker", "run", "--detach"]
    assert docker_run[-1] == "img:1"  # the image is the final positional arg (its entrypoint runs)
    assert ["--name", "panopticon-t1"] == docker_run[3:5]
    assert "PANOPTICON_SERVICE_URL=http://svc:8000" in docker_run
    assert "PANOPTICON_TASK_ID=t1" in docker_run
    assert "PANOPTICON_CONTAINER_ID=panopticon-t1" in docker_run
    assert "PANOPTICON_RUNNER_ID=r1" in docker_run
    # container -> host addressing so the container can reach the task service
    assert docker_run[docker_run.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    # the tmux session (on the default `panopticon` socket) shares the container name; its
    # pane execs the in-container agent launcher (so `tmux attach` reaches the live agent)
    assert tmux_new[:4] == ["tmux", "-L", "panopticon", "new-session"]
    assert tmux_new[tmux_new.index("-s") + 1] == "panopticon-t1"
    # the pane execs in as the unprivileged `panopticon` user (so the agent's whoami isn't root)
    assert tmux_new[-10:] == [
        "docker", "exec", "--interactive", "--tty", "--user", "panopticon", "panopticon-t1",
        "python", "-m", "panopticon.container.agent",
    ]


def test_spawn_reports_starting_then_awaiting_via_the_progress_callback() -> None:
    runner = LocalRunner("http://svc:8000", run=_Recorder())
    phases: list[LifecyclePhase] = []
    runner.spawn("t1", progress=phases.append)
    # STARTING just before `docker run`, AWAITING once the container + tmux session are up
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]


class _ReturningRecorder(_Recorder):
    """A recorder whose calls return a canned stdout (for parsing ``docker ps`` output)."""

    def __init__(self, output: str) -> None:
        super().__init__()
        self._output = output

    def __call__(self, args: Sequence[str], *, check: bool = True, interactive: bool = False, verbose: bool = False) -> str:
        super().__call__(args, check=check, interactive=interactive)
        return self._output


def test_is_running_queries_docker_ps_by_container_name() -> None:
    rec = _ReturningRecorder("panopticon-t1\n")
    runner = LocalRunner("http://svc:8000", run=rec)
    assert runner.is_running("t1") is True
    (ps, check), = rec.calls
    assert ps == ["docker", "ps", "--filter", "name=^panopticon-t1$", "--format", "{{.Names}}"]
    assert check is False  # tolerate a daemon hiccup rather than raise


def test_is_running_is_false_when_no_container_is_listed() -> None:
    runner = LocalRunner("http://svc:8000", run=_Recorder())  # records, returns "" → not running
    assert runner.is_running("t1") is False


def test_has_session_lists_the_tmux_server_and_matches_the_session_name() -> None:
    rec = _ReturningRecorder("panopticon-t1\npanopticon-t2\n")  # two sessions on the server
    runner = LocalRunner("http://svc:8000", run=rec)
    assert runner.has_session("t1") is True
    (ls, check), = rec.calls
    assert ls == ["tmux", "-L", "panopticon", "list-sessions", "-F", "#{session_name}"]
    assert check is False  # an empty list (or no server at all) just means "no session", not an error


def test_has_session_is_false_when_the_session_is_absent() -> None:
    # No server running (e.g. after `make stop`) → list-sessions prints nothing → not a session.
    assert LocalRunner("http://svc:8000", run=_Recorder()).has_session("t1") is False
    # A server with *other* sessions but not this task's is still a miss (no substring false-match).
    runner = LocalRunner("http://svc:8000", run=_ReturningRecorder("panopticon-t10\n"))
    assert runner.has_session("t1") is False


def test_spawn_runs_container_unprivileged_as_the_invoking_user() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    # the entrypoint adopts these and drops to the `panopticon` user (no root, no bare numeric uid)
    assert f"PANOPTICON_PUID={os.getuid()}" in docker_run
    assert f"PANOPTICON_PGID={os.getgid()}" in docker_run
    assert "--user" not in docker_run  # adoption happens in the entrypoint, not via docker --user


def test_spawn_user_can_be_overridden() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", user="1234:5678", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_PUID=1234" in docker_run and "PANOPTICON_PGID=5678" in docker_run


def test_spawn_without_docker_in_docker_is_not_privileged() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "--privileged" not in docker_run
    assert "PANOPTICON_DOCKER_IN_DOCKER=1" not in docker_run


def test_spawn_with_docker_in_docker_runs_privileged_and_flags_the_entrypoint() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1", docker_in_docker=True)
    docker_run = rec.calls[2][0]
    assert "--privileged" in docker_run  # nested daemon needs it (repo capability, ADR 0005)
    assert "panopticon-dind-t1:/var/lib/docker" in docker_run  # per-task docker layer cache
    assert "PANOPTICON_DOCKER_IN_DOCKER=1" in docker_run  # entrypoint starts dockerd


def test_extra_env_is_forwarded() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", extra_env={"PANOPTICON_RECONNECT_BACKOFF": "0.5"}, run=rec).spawn("t1")
    assert "PANOPTICON_RECONNECT_BACKOFF=0.5" in rec.calls[2][0]


def test_spawn_injects_repo_env_file() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn(
        "t1", env_file="/secrets/r1.env"
    )
    docker_run = rec.calls[2][0]
    assert docker_run[docker_run.index("--env-file") + 1] == "/secrets/r1.env"


def test_spawn_omits_secret_flags_when_repo_has_none() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert "--env-file" not in docker_run  # no API-key env-file
    # (the per-task config volume is always mounted — that's not a per-repo secret)


def test_spawn_mounts_the_per_task_clone_as_the_workspace() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1", workspace="/tasks/t1")
    docker_run = rec.calls[2][0]
    assert "/tasks/t1:/workspace" in docker_run  # the per-task clone, read-write (ADR 0011)
    assert docker_run[docker_run.index("--workdir") + 1] == "/workspace"  # the agent's working dir


def test_spawn_mounts_a_per_task_config_volume_for_claude_history() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    # a task-scoped named volume at the config dir → claude's transcripts survive respawn/recreate
    assert "panopticon-config-t1:/home/panopticon/.claude" in docker_run


def test_spawn_passes_initial_prompt_as_env_var() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", run=rec)
    runner.spawn("t1", initial_prompt="review your plan")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_INITIAL_PROMPT=review your plan" in docker_run


def test_spawn_passes_turn_as_env_var() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", run=rec)
    runner.spawn("t1", turn="agent")
    docker_run = rec.calls[2][0]
    assert "PANOPTICON_TASK_TURN=agent" in docker_run


def test_spawn_omits_turn_env_var_when_not_set() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    docker_run = rec.calls[2][0]
    assert not any("PANOPTICON_TASK_TURN" in arg for arg in docker_run)


def test_spawn_uses_the_composed_image_when_given_else_the_base() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc", image="panopticon-base", run=rec)
    runner.spawn("t1")  # no override → base
    assert rec.calls[2][0][-1] == "panopticon-base"
    runner.spawn("t2", image="panopticon-github-peer-reviewed-r1")  # composed image (ADR 0005)
    # each spawn emits 4 calls (kill-session, rm, run, tmux); t2's docker run is calls[6]
    assert rec.calls[6][0][-1] == "panopticon-github-peer-reviewed-r1"


def test_stop_kills_session_and_force_removes_container_idempotently() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).stop("panopticon-t1")
    assert (["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"], False) in rec.calls
    assert (["docker", "rm", "--force", "panopticon-t1"], False) in rec.calls


def test_delete_workspace_contents_runs_root_container_to_empty_directory() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", image="panopticon-base", run=rec).delete_workspace_contents("/tasks/t1")
    assert rec.calls == [(
        [
            "docker", "run", "--rm",
            "--entrypoint", "/bin/sh",
            "--volume", "/tasks/t1:/cleanup",
            "panopticon-base",
            "-c", "find /cleanup -mindepth 1 -delete",
        ],
        True,
    )]


def test_tmux_socket_can_be_overridden() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", tmux_socket="panopt", run=rec).spawn("t1")
    assert rec.calls[3][0][:4] == ["tmux", "-L", "panopt", "new-session"]  # kill-session, rm, run, tmux


# -- integration: real docker + tmux ------------------------------------------------

_HAVE_DOCKER_TMUX = bool(shutil.which("docker") and shutil.which("tmux"))


def _docker_running() -> bool:
    return _HAVE_DOCKER_TMUX and subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon + tmux")
def test_spawn_and_stop_real_container_and_session() -> None:
    image = "panopticon-itest:latest"
    socket = "panopticon-itest"
    subprocess.run(
        ["docker", "build", "--tag", image, "-"],
        # a `panopticon` user so the agent pane's `docker exec --user panopticon` resolves
        input='FROM alpine\nRUN adduser -D -u 1000 panopticon\nENTRYPOINT ["sleep", "3600"]\n',
        text=True, check=True, capture_output=True,
    )
    runner = LocalRunner(
        "http://unused", image=image, runner_id="itest", agent_command=["sh"], tmux_socket=socket
    )
    cid = "panopticon-itest1"
    try:
        assert runner.spawn("itest1") == cid
        running = ""
        for _ in range(50):  # `docker run -d` returns once running; poll defensively
            running = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", cid],
                capture_output=True, text=True,
            ).stdout.strip()
            if running == "true":
                break
            time.sleep(0.1)
        assert running == "true"
        assert subprocess.run(
            ["tmux", "-L", socket, "has-session", "-t", cid], capture_output=True
        ).returncode == 0

        runner.stop(cid)
        assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0
        assert subprocess.run(
            ["tmux", "-L", socket, "has-session", "-t", cid], capture_output=True
        ).returncode != 0
    finally:
        subprocess.run(["docker", "rm", "--force", cid], capture_output=True)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", image], capture_output=True)


class _FakeClient:
    """Stands in for TaskServiceClient: maps the task to its repo + that repo's secret refs."""

    def __init__(self, repo: dict[str, object]) -> None:
        self._repo = repo

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"id": task_id, "repo_id": "r1"}

    def get_repo(self, repo_id: str) -> dict[str, object]:
        return self._repo


def test_cli_preps_the_workspace_then_spawns_with_secrets_and_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panopticon.sessionservice import __main__ as cli
    from panopticon.sessionservice.__main__ import main as cli_main

    rec = _Recorder()
    fake = _FakeClient(
        {"id": "r1", "git_url": "https://forge/r1.git", "env_file": "/secrets/r1.env"}
    )
    # The clone cache and per-task clones roots are the base-dir defaults (no per-path flags); the
    # defaults are import-time constants, so point them at tmp dirs by patching them in place.
    cache_root, tasks_root = tmp_path / "cache", tmp_path / "tasks"
    monkeypatch.setattr(cli, "CLONE_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(cli, "TASKS_DIR", str(tasks_root))
    cid = cli_main(
        ["t1", "--service-url", "http://svc:9", "--image", "img:2"],
        run=rec, client=fake,  # type: ignore[arg-type]
    )
    assert cid == "panopticon-t1"
    cmds = [c for c, _ in rec.calls]
    # spawn-prep cloned the per-task checkout (ADR 0011) before launching the container
    assert ["git", "clone", "--local", str(cache_root / "r1"), str(tasks_root / "t1")] in cmds
    docker_run = next(c for c in cmds if c[:2] == ["docker", "run"])
    assert "PANOPTICON_SERVICE_URL=http://svc:9" in docker_run
    assert docker_run[-1] == "img:2"
    assert docker_run[docker_run.index("--env-file") + 1] == "/secrets/r1.env"  # repo's secrets
    assert f"{tasks_root}/t1:/workspace" in docker_run  # the per-task clone mounted as /workspace
