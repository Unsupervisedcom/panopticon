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

from collections.abc import Mapping

from panopticon.sessionservice.runner import Runner

#: Default composed image (base layer, ADR 0005); built in a later PR of this slice.
DEFAULT_IMAGE = "panopticon-base"

#: Lets the container reach the host task service (container→host addressing, ADR 0008).
#: ``host-gateway`` maps to the host's gateway IP; the service binds 0.0.0.0.
HOST_GATEWAY = "host.docker.internal:host-gateway"

#: Dedicated tmux server socket for panopticon's task sessions — isolates them from the
#: operator's own tmux and gives the terminal controller a known place to `tmux attach`.
TMUX_SOCKET = "panopticon"

#: Where a repo's OAuth credential volume is mounted in the task container (ADR 0007). The
#: agent layer points its CLI's config dir here (Slice 6); kept generic so it isn't CLI-specific.
CREDS_MOUNT = "/creds"


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
        agent_command: Sequence[str] = ("python", "-m", "panopticon.container.agent"),
        tmux_socket: str | None = TMUX_SOCKET,
        extra_env: Mapping[str, str] | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        # What the tmux pane execs into the container: the in-container agent launcher (it
        # bootstraps the CLI then runs `claude`). `tmux attach` therefore reaches the live agent.
        self._agent_command = list(agent_command)
        self._tmux_socket = tmux_socket  # isolate panopticon's tmux server when set (-L)
        self._extra_env = dict(extra_env or {})
        self._run = run

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def spawn(self, task_id: str, *, env_file: str | None = None, creds_volume: str | None = None) -> str:
        """Spawn the task container. ``env_file``/``creds_volume`` are the task's repo's secret
        references (ADR 0007), injected at launch — never baked into the image."""
        # The container name doubles as the tmux session name, so stop() needs only the id.
        container = f"panopticon-{task_id}"
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_CONTAINER_ID": container,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            **self._extra_env,
        }
        docker_run = [
            "docker", "run", "--detach",
            "--name", container,
            "--label", f"panopticon.task={task_id}",
            "--add-host", HOST_GATEWAY,
        ]
        if env_file:  # per-repo API-key secrets, injected at run (not in the image)
            docker_run += ["--env-file", env_file]
        if creds_volume:  # per-repo OAuth creds volume, mounted at a generic path
            docker_run += ["--volume", f"{creds_volume}:{CREDS_MOUNT}"]
        for key, value in env.items():
            docker_run += ["--env", f"{key}={value}"]
        docker_run.append(self._image)  # the image's entrypoint runs (no command override)
        self._run(docker_run)
        # `docker run --detach` returns once the container is running, so the pane can exec in.
        self._run(
            self._tmux(
                "new-session", "-d", "-s", container,
                "docker", "exec", "--interactive", "--tty", container, *self._agent_command,
            )
        )
        return container

    def stop(self, container_id: str) -> None:
        # Idempotent: tolerate an already-gone session/container.
        self._run(self._tmux("kill-session", "-t", container_id), check=False)
        self._run(["docker", "rm", "--force", container_id], check=False)

    def login(self, creds_volume: str, command: Sequence[str]) -> None:
        """Run an interactive container with a repo's creds volume mounted, to populate it
        (ADR 0007's generalized `login`). ``command`` is the CLI's login invocation (e.g.
        ``claude``); `CLAUDE_CONFIG_DIR` points claude at the mounted volume so its OAuth creds
        land there. The named volume is created on first use and persists across task restarts."""
        self._run(
            ["docker", "run", "--interactive", "--tty", "--rm",
             "--volume", f"{creds_volume}:{CREDS_MOUNT}",
             "--env", f"CLAUDE_CONFIG_DIR={CREDS_MOUNT}",
             self._image, *command],
            check=False,
        )
