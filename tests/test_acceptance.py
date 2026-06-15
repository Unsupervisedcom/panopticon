"""Slice 2 acceptance: the LocalRunner spawns a real task container from the base image; the
container connects back to a real (in-process) task service, registers, sets its slug, and
heartbeats; killing it freezes liveness. Skipped without a working docker daemon + tmux.

Builds the base image and runs a real container — no LLM (the entrypoint's agent step is a
stay-alive heartbeat loop)."""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from panopticon.core.models import Repo
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

_IMAGE = "panopticon-acceptance:latest"
_TMUX_SOCKET = "panopticon-acceptance"


def _docker_running() -> bool:
    import shutil

    if not (shutil.which("docker") and shutil.which("tmux")):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[TaskService, int]]:
    """A real TaskService served by uvicorn in a background thread, on 0.0.0.0:<port>."""
    import uvicorn

    service = TaskService(SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    port = _free_port()
    config = uvicorn.Config(create_app(service), host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        yield service, port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon + tmux")
def test_runner_spawns_real_container_that_registers_and_loses_liveness(
    served: tuple[TaskService, int],
) -> None:
    service, port = served
    subprocess.run(
        ["docker", "build", "--tag", _IMAGE, "--file", "docker/Dockerfile", "."],
        check=True, capture_output=True,
    )
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task_id = service.create_task("r1", "spike").id

    runner = LocalRunner(
        f"http://host.docker.internal:{port}",
        image=_IMAGE,
        runner_id="acceptance",
        tmux_socket=_TMUX_SOCKET,
        # The pane normally execs the agent launcher, but that runs `claude`, which the bare
        # base image lacks (it arrives in a later image layer) — and "no LLMs in tests" anyway.
        # This acceptance test only proves liveness + a live tmux pane to attach to, so a
        # stay-alive shell stands in for the agent.
        agent_command=["bash"],
        extra_env={"PANOPTICON_HEARTBEAT_INTERVAL": "0.5", "PANOPTICON_PROPOSED_SLUG": "acc-slug"},
    )
    container = runner.spawn(task_id)
    try:
        # 1. the container connected back and registered (liveness)
        reg = None
        for _ in range(120):
            regs = service.registrations(task_id)
            if regs:
                reg = regs[0]
                break
            time.sleep(0.25)
        assert reg is not None, "container never registered: " + subprocess.run(
            ["docker", "logs", container], capture_output=True, text=True
        ).stderr

        # 2. the slug hook ran in-container
        for _ in range(40):
            if service.get_task(task_id).slug == "acc-slug":
                break
            time.sleep(0.25)
        assert service.get_task(task_id).slug == "acc-slug"

        # 3. tmux session exists (operator could `tmux attach`)
        assert subprocess.run(
            ["tmux", "-L", _TMUX_SOCKET, "has-session", "-t", container], capture_output=True
        ).returncode == 0

        # 4. it is heartbeating: last_seen advances
        before = service.registrations(task_id)[0].last_seen
        time.sleep(1.5)
        assert service.registrations(task_id)[0].last_seen != before, "no heartbeats"

        # 5. killing the container freezes liveness (no deregister, last_seen stops advancing)
        subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
        time.sleep(0.5)
        frozen = service.registrations(task_id)[0].last_seen
        time.sleep(1.5)
        assert service.registrations(task_id)[0].last_seen == frozen, "heartbeats continued after kill"
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        subprocess.run(["tmux", "-L", _TMUX_SOCKET, "kill-server"], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", _IMAGE], capture_output=True)
