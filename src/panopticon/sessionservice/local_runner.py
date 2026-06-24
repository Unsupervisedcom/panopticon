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
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

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
    ``docker run -it`` (the ``login`` shell), where capturing would leave its TTY with no real input
    and hang."""

    def __call__(self, args: Sequence[str], *, check: bool = True, interactive: bool = False) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True, interactive: bool = False) -> str:
    if interactive:  # inherit the terminal so the container's TTY is the operator's (no capture)
        subprocess.run(list(args), check=check)
        return ""
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


class PrefillLauncher(Protocol):
    """Launches the input-box prefill poller (``sessionservice.prefill``) **detached** for a tmux
    ``session``, reading ``prompt_file``. Injectable so ``spawn`` stays fast + unit-testable."""

    def __call__(self, session: str, prompt_file: str, *, socket: str | None) -> None: ...


def _launch_prefill(session: str, prompt_file: str, *, socket: str | None) -> None:  # pragma: no cover - detaches a real subprocess
    """Spawn ``python -m panopticon.sessionservice.prefill`` in its own session (``setsid``-style)
    so it outlives ``spawn`` and never blocks it; it polls the pane and pastes the prompt, then
    removes ``prompt_file`` itself. Fire-and-forget — the prefill is best-effort (see ``prefill``)."""
    argv = [sys.executable, "-m", "panopticon.sessionservice.prefill", session, prompt_file]
    if socket:
        argv += ["--socket", socket]
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
        run: CommandRunner = _subprocess_run,
        prefill: PrefillLauncher = _launch_prefill,
    ) -> None:
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        # Run the task container unprivileged as the invoking user (uid:gid), so it can't act as
        # root on the host and its writes to the mounted workspace are owned by the operator.
        self._user = user if user is not None else _invoking_user()
        # What the tmux pane execs into the container: the in-container agent launcher (it
        # bootstraps the CLI then runs `claude`). `tmux attach` therefore reaches the live agent.
        self._agent_command = list(agent_command)
        self._tmux_socket = tmux_socket  # isolate panopticon's tmux server when set (-L)
        self._extra_env = dict(extra_env or {})
        self._run = run
        self._prefill = prefill

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        creds_volume: str | None = None,
        workspace: str | None = None,
        image: str | None = None,
        docker_in_docker: bool = False,
        memo: str | None = None,
    ) -> str:
        """Spawn the task container. ``env_file``/``creds_volume`` are the task's repo's secret
        references (ADR 0007), injected at launch — never baked into the image. ``workspace`` is the
        task's per-task clone on the host (ADR 0011), bind-mounted read-write at ``/workspace`` as
        the agent's working dir. ``image`` overrides the default base with the task's composed image
        (base → workflow → repo, ADR 0005); ``None`` uses the configured base. ``docker_in_docker``
        (the repo's ``capabilities``) runs the container ``--privileged`` and tells the entrypoint to
        start a nested Docker daemon — a trust escalation, opt-in per repo. ``memo`` (the
        task's free-text intent) is pre-filled into claude's input box on a **first** spawn, left
        unsent — see :func:`_maybe_prefill`."""
        # The container name doubles as the tmux session name, so stop() needs only the id.
        container = f"panopticon-{task_id}"
        # Decide *before* `docker run` (which creates the config volume) whether this is the task's
        # first spawn — only then do we prefill, so a respawn doesn't paste into a --continue'd box.
        first_spawn = self._wants_prefill(memo) and not self._config_volume_exists(task_id)
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
        docker_run = [
            "docker", "run", "--detach",
            "--name", container,
            "--label", f"panopticon.task={task_id}",
            "--add-host", HOST_GATEWAY,
        ]
        if docker_in_docker:  # privileged nested Docker daemon (repo capability); entrypoint starts it
            docker_run.append("--privileged")
            env["PANOPTICON_DOCKER_IN_DOCKER"] = "1"
        if env_file:  # per-repo API-key secrets, injected at run (not in the image)
            docker_run += ["--env-file", env_file]
        if creds_volume:  # per-repo OAuth creds volume, mounted at a generic path
            docker_run += ["--volume", f"{creds_volume}:{CREDS_MOUNT}"]
        if workspace:  # the per-task clone — the agent's writable working dir (ADR 0011)
            docker_run += ["--volume", f"{workspace}:{WORKSPACE_MOUNT}", "--workdir", WORKSPACE_MOUNT]
        # Per-task config volume: persists claude's session history across respawn/recreate (the
        # transcripts live in the config dir, which is otherwise thrown away with the container).
        docker_run += ["--volume", f"panopticon-config-{task_id}:{CONFIG_MOUNT}"]
        for key, value in env.items():
            docker_run += ["--env", f"{key}={value}"]
        docker_run.append(image or self._image)  # composed image if given, else base; its entrypoint runs
        # Clear any stale container of this name first — a prior run that exited, or a respawn
        # (dashboard `R` releases the claim but doesn't stop the dead container) — so `--name`
        # doesn't fail "name already in use". Makes spawn idempotent. (`stop()` also removes it.)
        self._run(["docker", "rm", "--force", container], check=False)
        self._run(docker_run)
        # `docker run --detach` returns once the container is running (the entrypoint has remapped +
        # dropped), so the pane execs in as the unprivileged `panopticon` user — `tmux attach` and
        # the agent's `whoami` see that named user, not root.
        self._run(
            self._tmux(
                "new-session", "-d", "-s", container,
                "docker", "exec", "--interactive", "--tty", "--user", CONTAINER_USER,
                container, *self._agent_command,
            )
        )
        if first_spawn and memo is not None:
            self._maybe_prefill(container, memo)
        return container

    @staticmethod
    def _wants_prefill(memo: str | None) -> bool:
        """Whether a memo is worth pre-filling: non-empty and not opted out via
        ``PANOPTICON_NO_PREFILL`` (the env knob the detached poller also honours)."""
        return bool(memo and memo.strip()) and not os.environ.get("PANOPTICON_NO_PREFILL")

    def _config_volume_exists(self, task_id: str) -> bool:
        """True if the per-task config volume is already present — i.e. the task has been spawned
        before (a respawn). It's the same persisted state ``claude --continue`` keys on, so gating
        the prefill on it keeps the two consistent: prefill a brand-new box, never a continued one."""
        return bool(self._run(
            ["docker", "volume", "inspect", f"panopticon-config-{task_id}"], check=False
        ).strip())

    def _maybe_prefill(self, session: str, memo: str) -> None:
        """Write the memo to a throwaway file and launch the detached prefill poller against
        the task's tmux ``session``. The poller pastes it into claude's input box, unsent, then
        removes the file. Best-effort: a launch failure must not fail the spawn."""
        fd, prompt_file = tempfile.mkstemp(prefix=f"panopticon-prefill-{session}-", suffix=".txt")
        with os.fdopen(fd, "w") as handle:
            handle.write(memo)
        try:
            self._prefill(session, prompt_file, socket=self._tmux_socket)
        except OSError:  # couldn't even launch the poller — drop the temp file, leave the box empty
            try:
                os.unlink(prompt_file)
            except OSError:
                pass

    def stop(self, container_id: str) -> None:
        # Idempotent: tolerate an already-gone session/container.
        self._run(self._tmux("kill-session", "-t", container_id), check=False)
        self._run(["docker", "rm", "--force", container_id], check=False)

    def login(self, creds_volume: str, command: Sequence[str]) -> None:
        """Run an interactive container with a repo's creds volume mounted, to populate it
        (ADR 0007's generalized `login`). ``command`` is the CLI's login invocation (e.g.
        ``claude``); `CLAUDE_CONFIG_DIR` points claude at the mounted volume so its OAuth creds
        land there. The named volume is created on first use and persists across task restarts.

        ``command`` is passed through the image's entrypoint, which adopts the same invoking user as
        the task container (``PANOPTICON_PUID``/``PGID``), chowns ``/creds`` to it, then drops to it
        before running ``command``. So the creds it writes are owned by that user — the task
        container, running as the same user, can then read and refresh them.

        Propagating the fresh creds to the repo's already-running task containers is handled by the
        caller restarting them (``sessionservice.restart``), not here — this only writes the volume."""
        puid, _, pgid = self._user.partition(":")
        self._run(
            ["docker", "run", "--interactive", "--tty", "--rm",
             "--env", f"PANOPTICON_PUID={puid}", "--env", f"PANOPTICON_PGID={pgid}",
             "--volume", f"{creds_volume}:{CREDS_MOUNT}",
             "--env", f"CLAUDE_CONFIG_DIR={CREDS_MOUNT}",
             self._image, *command],
            check=False,
            interactive=True,  # attach the operator's terminal to the container's TTY (else it hangs)
        )
