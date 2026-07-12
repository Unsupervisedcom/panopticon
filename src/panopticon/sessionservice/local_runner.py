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

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from panopticon.core.dirs import secrets_file_path
from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.runner import Runner

#: Default composed image (base layer, ADR 0005); built in a later PR of this slice.
DEFAULT_IMAGE = "panopticon-base"

#: Lets the container reach the host task service (container→host addressing, ADR 0008).
#: ``host-gateway`` maps to the host's gateway IP; the service binds 0.0.0.0.
HOST_GATEWAY = "host.docker.internal:host-gateway"

#: Dedicated tmux server socket for panopticon's task sessions — isolates them from the
#: operator's own tmux and gives the terminal controller a known place to `tmux attach`.
TMUX_SOCKET = "panopticon"

#: Where a task's per-task clone is mounted — the one stable, writable path the agent works in
#: for the whole task (ADR 0011): planning, then coding on its branch once provisioned.
WORKSPACE_MOUNT = "/workspace"

#: The unprivileged in-container account the task runs as (created in the base image). The
#: entrypoint remaps it to the invoking user's uid/gid at start; `docker exec` for the agent pane
#: names it so the pane runs as that same user (ADR 0008 / the unprivileged-user work).
CONTAINER_USER = "panopticon"

#: The agent CLI's config dir inside the container (matches the image's HOME + `agent.py`'s
#: ``Path.home()/.claude``). A **per-task** named volume is mounted here so claude's history
#: (its session transcripts) survives respawn/recreate — the container layer is thrown away each
#: spawn, but the volume persists. Per-task (not per-repo) so concurrent tasks don't share state.
CONFIG_MOUNT = "/home/panopticon/.claude"


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on non-zero exit.

    ``interactive`` attaches the caller's terminal (stdin/stdout/stderr) instead of capturing — for
    an interactive ``docker run -it``, where capturing would leave its TTY with no real input and
    hang. ``verbose`` also inherits the caller's streams but is for non-interactive commands whose
    output should be visible in the runner's tmux session (e.g. ``docker build``)."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str: ...


def _subprocess_run(
    args: Sequence[str], *, check: bool = True, interactive: bool = False, verbose: bool = False
) -> str:
    if (
        interactive or verbose
    ):  # inherit streams: TTY attachment (interactive) or visible build output (verbose)
        subprocess.run(list(args), check=check)
        return ""
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def _invoking_user() -> str:
    """The ``uid:gid`` of the host process invoking the runner — passed to the container (as
    ``PANOPTICON_PUID``/``PGID``) for its entrypoint to adopt, so the task runs **unprivileged** as
    that user and the files it writes to the bind-mounted ``/workspace`` (the per-task clone) are
    owned by the operator, not root. Matching the workspace owner's uid also sidesteps git's
    "dubious ownership" guard on the mounted checkout."""
    return f"{os.getuid()}:{os.getgid()}"


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
        user: str | None = None,
        secrets_dir: str | Path | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        # Root the repo's `env_file` name resolves against — this host's local secrets dir, so a
        # remote runner uses its own secrets (the stored value is host-agnostic; ADR 0007). None =
        # resolve the host's secrets dir dynamically at spawn.
        self._secrets_dir = secrets_dir
        # Run the task container unprivileged as the invoking user (uid:gid), so it can't act as
        # root on the host and its writes to the mounted workspace are owned by the operator.
        self._user = user if user is not None else _invoking_user()
        # What the tmux pane execs into the container: the in-container agent launcher (it
        # bootstraps the CLI then runs `claude`). `tmux attach` therefore reaches the live agent.
        self._agent_command = list(agent_command)
        self._tmux_socket = tmux_socket  # isolate panopticon's tmux server when set (-L)
        self._extra_env = dict(extra_env or {})
        self._run = run

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

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
        """Spawn the task container. ``env_file`` is the task's repo's secret reference (ADR
        0007) — a name **relative to this runner's secrets dir** (:data:`SECRETS_DIR`), resolved
        host-locally and injected at launch (``--env-file``), never baked into the image and never
        crossing the wire (so a remote runner uses its own host's secrets). ``workspace`` is the
        task's per-task clone on the host (ADR 0011), bind-mounted read-write at ``/workspace`` as
        the agent's working dir. ``image`` overrides the default base with the task's composed image
        (base → workflow → repo, ADR 0005); ``None`` uses the configured base. ``docker_in_docker``
        (the repo's ``capabilities``) runs the container ``--privileged`` and tells the entrypoint to
        start a nested Docker daemon — a trust escalation, opt-in per repo. ``initial_prompt``
        is passed as a positional arg to ``claude`` on the first run (no prior session) via the
        ``PANOPTICON_INITIAL_PROMPT`` env var; the agent starts autonomously without waiting for
        user input. ``turn`` is the
        task's current turn (``"agent"`` or ``"user"``); passed as ``PANOPTICON_TASK_TURN`` so the
        agent launcher can send :data:`~panopticon.container.agent.INTERRUPT_PROMPT` on respawn when
        the agent holds the turn. ``starting_model`` is the model the agent should start with
        (e.g. ``"opus"``); passed as ``PANOPTICON_STARTING_MODEL`` so the agent launcher can pass
        ``--model`` to ``claude`` on first launch. ``progress`` (optional) is called with each spawn
        phase the runner passes through (``STARTING`` before ``docker run``, ``AWAITING`` once the
        tmux session is up) so the caller can surface it — see
        :class:`~panopticon.core.models.LifecyclePhase`."""

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        # The container name doubles as the tmux session name, so stop() needs only the id.
        container = f"panopticon-{task_id}"
        puid, _, pgid = self._user.partition(":")
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_CONTAINER_ID": container,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            # The entrypoint adopts these: it remaps the `panopticon` user to the invoking uid/gid
            # and drops to it (so the task runs unprivileged, owning what it writes to /workspace).
            "PANOPTICON_PUID": puid,
            "PANOPTICON_PGID": pgid,
            **self._extra_env,
        }
        if initial_prompt:
            # The agent launcher reads this and passes it as a positional arg to `claude` on the
            # first run (no prior session), so the agent's first action is to process the prompt.
            env["PANOPTICON_INITIAL_PROMPT"] = initial_prompt
        if turn:
            env["PANOPTICON_TASK_TURN"] = turn
        if starting_model:
            env["PANOPTICON_STARTING_MODEL"] = starting_model
        docker_run = [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--label",
            f"panopticon.task={task_id}",
            "--add-host",
            HOST_GATEWAY,
        ]
        if (
            docker_in_docker
        ):  # privileged nested Docker daemon (repo capability); entrypoint starts it
            docker_run.append("--privileged")
            docker_run += ["--volume", f"panopticon-dind-{task_id}:/var/lib/docker"]
            env["PANOPTICON_DOCKER_IN_DOCKER"] = "1"
        if env_path := secrets_file_path(env_file, secrets_dir=self._secrets_dir):
            docker_run += ["--env-file", env_path]  # per-repo secrets, resolved host-locally
        if workspace:  # the per-task clone — the agent's writable working dir (ADR 0011)
            docker_run += [
                "--volume",
                f"{workspace}:{WORKSPACE_MOUNT}",
                "--workdir",
                WORKSPACE_MOUNT,
            ]
        # Per-task config volume: persists claude's session history across respawn/recreate (the
        # transcripts live in the config dir, which is otherwise thrown away with the container).
        docker_run += ["--volume", f"panopticon-config-{task_id}:{CONFIG_MOUNT}"]
        for key, value in env.items():
            docker_run += ["--env", f"{key}={value}"]
        docker_run.append(
            image or self._image
        )  # composed image if given, else base; its entrypoint runs
        # Clear any stale tmux session + container first — handles both a prior exited run and a
        # live force-respawn (dashboard `R` kills and restarts). Both are no-ops when nothing
        # exists, so spawn is fully idempotent. (`stop()` does the same pair.)
        self._run(self._tmux("kill-session", "-t", container), check=False)
        self._run(["docker", "rm", "--force", container], check=False)
        _report(LifecyclePhase.STARTING)  # docker run + the tmux session coming up
        self._run(docker_run)
        # `docker run --detach` returns once the container is running (the entrypoint has remapped +
        # dropped), so the pane execs in as the unprivileged `panopticon` user — `tmux attach` and
        # the agent's `whoami` see that named user, not root.
        self._run(
            self._tmux(
                "new-session",
                "-d",
                "-s",
                container,
                "docker",
                "exec",
                "--interactive",
                "--tty",
                "--user",
                CONTAINER_USER,
                container,
                *self._agent_command,
            )
        )
        _report(LifecyclePhase.AWAITING)  # container + tmux up; waiting for its /live registration
        return container

    def is_running(self, task_id: str) -> bool:
        """Whether the task's container is currently running on this host's Docker daemon.

        A ``docker ps`` (running containers only) filtered to the task's container name: empty
        output means the container is gone or exited — i.e. the task is **down** and should be
        respawned. Used by the host daemon to reconcile a claimed task that never came up (or
        died) into the displayed ``down`` status."""
        container = f"panopticon-{task_id}"
        names = self._run(
            ["docker", "ps", "--filter", f"name=^{container}$", "--format", "{{.Names}}"],
            check=False,
        )
        return bool(names.strip())

    def has_session(self, task_id: str) -> bool:
        """Whether the task's host tmux session exists on this runner's tmux server.

        Lists the panopticon tmux server's sessions and looks for ``panopticon-<id>``; an empty list
        (or no server at all) means the session is gone. We list-and-match rather than ``has-session``
        because the command runner reports stdout, not exit status, and ``has-session`` signals only
        through its exit code.

        Distinct from :meth:`is_running` (the *container*): a kill of the ``-L panopticon`` tmux server
        that *isn't* ``make stop`` — a crash, a manual ``tmux kill-server``, a single killed session —
        leaves the detached containers running, so a task can be ``is_running`` yet have **no session**:
        the orphan the host daemon self-heals by respawning. (``make stop`` itself now stops the task
        containers too, so it leaves nothing running — but the still-claimed task is likewise healed on
        the next start.)"""
        session = f"panopticon-{task_id}"
        sessions = self._run(self._tmux("list-sessions", "-F", "#{session_name}"), check=False)
        return session in sessions.splitlines()

    def delete_workspace_contents(self, path: str) -> None:
        """Delete all files inside ``path`` by running a throwaway root Docker container.

        A task container may write root-owned files (e.g. ``.mypy_cache`` before the
        entrypoint's uid remap, or via ``docker_in_docker``). This spawns a short-lived
        ``--rm`` container as root with ``path`` bind-mounted and deletes everything inside
        it, so the daemon can then ``rmtree`` the now-empty directory. Overrides the
        panopticon entrypoint (which would remap uid) so the container runs as root and can
        reach files it created. Raises on nonzero docker exit."""
        self._run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                "--volume",
                f"{path}:/cleanup",
                self._image,
                "-c",
                "find /cleanup -mindepth 1 -delete",
            ]
        )

    def stop(self, container_id: str) -> None:
        # Idempotent: tolerate an already-gone session/container.
        self._run(self._tmux("kill-session", "-t", container_id), check=False)
        self._run(["docker", "rm", "--force", container_id], check=False)
