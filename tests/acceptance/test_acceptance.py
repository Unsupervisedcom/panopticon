"""Slice 2 acceptance: the LocalRunner spawns a real task container from the base image; the
container connects back to a real (in-process) task service, registers (by holding the liveness
connection), and sets its slug; killing it drops the connection so liveness is reaped immediately.
Skipped without a working docker daemon + tmux.

Builds the base image and runs a real container — no LLM (the entrypoint's agent step is a
stay-alive liveness connection)."""

from __future__ import annotations

import asyncio
import importlib.resources
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import panopticon.docker as _docker_pkg
from panopticon.core.models import Repo
from panopticon.sessionservice.local_runner import LocalRunner, session_name
from panopticon.sessionservice.shell_runner import ShellRunner
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

    service = TaskService(
        SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path)
    )
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
    tmp_path: Path,
) -> None:
    service, port = served

    # Build a local wheel of the current source so the container image doesn't need a
    # published PyPI release (PANOPTICON_WHEEL triggers the dev-install path in the Dockerfile).
    repo_root = Path(__file__).parent.parent.parent
    wheel_out = tmp_path / "wheels"
    wheel_out.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", f"--out-dir={wheel_out}"],
        check=True,
        capture_output=True,
        cwd=repo_root,
    )
    (whl,) = list(wheel_out.glob("*.whl"))

    dockerfile_ref = importlib.resources.files(_docker_pkg) / "Dockerfile"
    with importlib.resources.as_file(dockerfile_ref) as dockerfile_path:
        ctx_whl = dockerfile_path.parent / whl.name
        shutil.copy(whl, ctx_whl)
        try:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--tag",
                    _IMAGE,
                    "--build-arg",
                    f"PANOPTICON_WHEEL={whl.name}",
                    "--file",
                    str(dockerfile_path),
                    str(dockerfile_path.parent),
                ],
                check=True,
                capture_output=True,
            )
        finally:
            ctx_whl.unlink(missing_ok=True)
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    task_id = asyncio.run(service.create_task("r1", "spike")).id

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
        extra_env={"PANOPTICON_PROPOSED_SLUG": "acc-slug"},
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
        assert reg is not None, (
            "container never registered: "
            + subprocess.run(["docker", "logs", container], capture_output=True, text=True).stderr
        )

        # 2. the slug hook ran in-container
        for _ in range(40):
            if asyncio.run(service.get_task(task_id)).slug == "acc-slug":
                break
            time.sleep(0.25)
        assert asyncio.run(service.get_task(task_id)).slug == "acc-slug"

        # 3. tmux session exists (operator could `tmux attach`)
        assert (
            subprocess.run(
                ["tmux", "-L", _TMUX_SOCKET, "has-session", "-t", container], capture_output=True
            ).returncode
            == 0
        )

        # 4. killing the container (SIGKILL) drops the liveness connection, so the service reaps
        #    the registration immediately — push, not a TTL age-out (the old model waited ~20s).
        subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
        reaped = False
        for _ in range(40):  # ~10s ceiling; in practice near-immediate on disconnect
            if not service.registrations(task_id):
                reaped = True
                break
            time.sleep(0.25)
        assert reaped, "liveness was not reaped after the container died (connection should drop)"
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        subprocess.run(["tmux", "-L", _TMUX_SOCKET, "kill-server"], capture_output=True)
        subprocess.run(["docker", "rmi", "--force", _IMAGE], capture_output=True)


_HAVE_TMUX_CURL = bool(shutil.which("tmux") and shutil.which("curl"))
_SHELL_SOCKET = "panopticon-shell-acceptance"


@pytest.mark.skipif(not _HAVE_TMUX_CURL, reason="needs tmux + curl")
def test_shell_task_registers_live_with_the_service(
    served: tuple[TaskService, int], tmp_path: Path
) -> None:
    # A shell task runs no container/agent, so ShellRunner holds a /live registration open for the
    # session's lifetime — proving the dashboard shows it *live* (not `awaiting`) while it runs, and
    # that the registration is reaped the instant the session ends.
    service, port = served
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    task_id = asyncio.run(service.create_task("r1", "spike")).id

    runner = ShellRunner(
        f"http://127.0.0.1:{port}", runner_id="shell-acc", tmux_socket=_SHELL_SOCKET
    )
    session = session_name(task_id)
    try:
        runner.spawn(task_id, script="sleep 30", workdir=str(tmp_path))
        registered = False
        for _ in range(100):  # the backgrounded curl connects → the service registers the task
            if service.registrations(task_id):
                registered = True
                break
            time.sleep(0.1)
        assert registered, "shell task never opened its /live registration"
    finally:
        runner.stop(session)  # kill the session → SIGHUP the pane group → the curl drops the stream
        subprocess.run(["tmux", "-L", _SHELL_SOCKET, "kill-server"], capture_output=True)

    reaped = False
    for _ in range(100):  # the dropped connection deregisters immediately (no TTL)
        if not service.registrations(task_id):
            reaped = True
            break
        time.sleep(0.1)
    assert reaped, "liveness was not reaped after the session ended"


@pytest.mark.skipif(not _HAVE_TMUX_CURL, reason="needs tmux + curl")
def test_shell_lib_drives_the_task_service(served: tuple[TaskService, int], tmp_path: Path) -> None:
    # The panopticon shell lib injected into every shell task lets its script drive the task over
    # REST. Here the script calls `panopticon_set_url` and the service reflects it — proving a shell
    # workflow can drive its own task without hand-rolling curl.
    service, port = served
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    task_id = asyncio.run(service.create_task("r1", "spike")).id

    runner = ShellRunner(
        f"http://127.0.0.1:{port}", runner_id="shell-acc", tmux_socket=_SHELL_SOCKET
    )
    try:
        runner.spawn(
            task_id,
            script="panopticon_set_url https://example.test/pr/1; sleep 30",
            workdir=str(tmp_path),
        )
        recorded = None
        for _ in range(100):  # the lib's PUT /url lands on the service
            recorded = asyncio.run(service.get_task(task_id)).url
            if recorded:
                break
            time.sleep(0.1)
        assert recorded == "https://example.test/pr/1", "shell lib did not drive the task service"
    finally:
        runner.stop(session_name(task_id))
        subprocess.run(["tmux", "-L", _SHELL_SOCKET, "kill-server"], capture_output=True)
