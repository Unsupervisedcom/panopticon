"""LocalRunner: unit tests pin the emitted docker/tmux commands; one integration test
exercises a real container + tmux session (skipped when docker/tmux are unavailable)."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Sequence

import pytest

from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.runner import Runner


class _Recorder:
    """An injectable CommandRunner that records calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append((list(args), check))
        return ""


def test_local_runner_is_a_runner() -> None:
    assert issubclass(LocalRunner, Runner)


def test_spawn_runs_detached_container_then_tmux_pane_execing_in() -> None:
    rec = _Recorder()
    runner = LocalRunner("http://svc:8000", image="img:1", runner_id="r1", run=rec)

    container_id = runner.spawn("t1")

    assert container_id == "panopticon-t1"
    (docker_run, _), (tmux_new, _) = rec.calls
    assert docker_run[:3] == ["docker", "run", "-d"]
    assert docker_run[-1] == "img:1"  # the image is the final positional arg (its entrypoint runs)
    assert ["--name", "panopticon-t1"] == docker_run[3:5]
    assert "PANOPTICON_SERVICE_URL=http://svc:8000" in docker_run
    assert "PANOPTICON_TASK_ID=t1" in docker_run
    assert "PANOPTICON_CONTAINER_ID=panopticon-t1" in docker_run
    assert "PANOPTICON_RUNNER_ID=r1" in docker_run
    # container -> host addressing so the container can reach the task service
    assert docker_run[docker_run.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    # the tmux session (on the default `panopticon` socket) shares the container name; its
    # pane execs an interactive shell in
    assert tmux_new[:4] == ["tmux", "-L", "panopticon", "new-session"]
    assert tmux_new[tmux_new.index("-s") + 1] == "panopticon-t1"
    assert tmux_new[-5:] == ["docker", "exec", "-it", "panopticon-t1", "bash"]


def test_extra_env_is_forwarded() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", extra_env={"PANOPTICON_HEARTBEAT_INTERVAL": "0.5"}, run=rec).spawn("t1")
    assert "PANOPTICON_HEARTBEAT_INTERVAL=0.5" in rec.calls[0][0]


def test_spawn_injects_repo_env_file_and_creds_mount() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn(
        "t1", env_file="/secrets/r1.env", creds_volume="panopticon-creds-r1"
    )
    docker_run = rec.calls[0][0]
    assert docker_run[docker_run.index("--env-file") + 1] == "/secrets/r1.env"
    assert "panopticon-creds-r1:/creds" in docker_run  # mounted at the generic creds path


def test_spawn_omits_secret_flags_when_repo_has_none() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).spawn("t1")
    assert "--env-file" not in rec.calls[0][0] and "-v" not in rec.calls[0][0]


def test_stop_kills_session_and_force_removes_container_idempotently() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", run=rec).stop("panopticon-t1")
    assert (["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"], False) in rec.calls
    assert (["docker", "rm", "-f", "panopticon-t1"], False) in rec.calls


def test_tmux_socket_can_be_overridden() -> None:
    rec = _Recorder()
    LocalRunner("http://svc", tmux_socket="panopt", run=rec).spawn("t1")
    assert rec.calls[1][0][:4] == ["tmux", "-L", "panopt", "new-session"]


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
        ["docker", "build", "-t", image, "-"],
        input='FROM alpine\nENTRYPOINT ["sleep", "3600"]\n',
        text=True, check=True, capture_output=True,
    )
    runner = LocalRunner(
        "http://unused", image=image, runner_id="itest", shell="sh", tmux_socket=socket
    )
    cid = "panopticon-itest1"
    try:
        assert runner.spawn("itest1") == cid
        running = ""
        for _ in range(50):  # `docker run -d` returns once running; poll defensively
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", cid],
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
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


class _FakeClient:
    """Stands in for TaskServiceClient: maps the task to its repo + that repo's secret refs."""

    def __init__(self, repo: dict[str, object]) -> None:
        self._repo = repo

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"id": task_id, "repo_id": "r1"}

    def get_repo(self, repo_id: str) -> dict[str, object]:
        return self._repo


def test_cli_spawns_with_service_url_and_image_and_injects_repo_secrets() -> None:
    from panopticon.sessionservice.__main__ import main as cli_main

    rec = _Recorder()
    fake = _FakeClient({"id": "r1", "env_file": "/secrets/r1.env", "creds_volume": "creds-r1"})
    cid = cli_main(
        ["t1", "--service-url", "http://svc:9", "--image", "img:2"], run=rec, client=fake  # type: ignore[arg-type]
    )
    assert cid == "panopticon-t1"
    docker_run = rec.calls[0][0]
    assert "PANOPTICON_SERVICE_URL=http://svc:9" in docker_run
    assert docker_run[-1] == "img:2"
    assert docker_run[docker_run.index("--env-file") + 1] == "/secrets/r1.env"  # repo's secrets
    assert "creds-r1:/creds" in docker_run
