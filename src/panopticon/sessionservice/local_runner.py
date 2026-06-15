"""Local Docker + tmux runner (ADR 0008): the real execution backend on one host.

Spawns a **detached** task container on the host Docker daemon and a **host tmux** session
whose pane execs an interactive shell into it; the container's own entrypoint connects back to
the task service for liveness. We shell out to the ``docker`` and ``tmux`` CLIs — the
interactive surface (the container's TTY living in a tmux pane, and the operator's
``tmux attach``) is inherently CLI, and the Python SDKs don't serve it (see the ADR 0008
review). The command executor is **injectable** so the runner is unit-testable without a
daemon. LLM-free — the agent runs inside the container.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Protocol

from panopticon.sessionservice.runner import Runner

#: Default composed image (base layer, ADR 0005); built in a later PR of this slice.
DEFAULT_IMAGE = "panopticon-base"


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on non-zero exit."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


class LocalRunner(Runner):
    """Runs task containers + host tmux on the local Docker daemon (one host)."""

    def __init__(
        self,
        service_url: str,
        *,
        image: str = DEFAULT_IMAGE,
        runner_id: str = "local",
        shell: str = "bash",
        tmux_socket: str | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        self._shell = shell  # the base image's interactive shell, exec'd in the tmux pane
        self._tmux_socket = tmux_socket  # isolate panopticon's tmux server when set (-L)
        self._run = run

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def spawn(self, task_id: str) -> str:
        # The container name doubles as the tmux session name, so stop() needs only the id.
        container = f"panopticon-{task_id}"
        self._run(
            [
                "docker", "run", "-d",
                "--name", container,
                "--label", f"panopticon.task={task_id}",
                "-e", f"PANOPTICON_SERVICE_URL={self._service_url}",
                "-e", f"PANOPTICON_TASK_ID={task_id}",
                "-e", f"PANOPTICON_CONTAINER_ID={container}",
                "-e", f"PANOPTICON_RUNNER_ID={self._runner_id}",
                self._image,
            ]
        )
        # `docker run -d` returns once the container is running, so the pane can exec into it.
        self._run(
            self._tmux(
                "new-session", "-d", "-s", container,
                "docker", "exec", "-it", container, self._shell,
            )
        )
        return container

    def stop(self, container_id: str) -> None:
        # Idempotent: tolerate an already-gone session/container.
        self._run(self._tmux("kill-session", "-t", container_id), check=False)
        self._run(["docker", "rm", "-f", container_id], check=False)
