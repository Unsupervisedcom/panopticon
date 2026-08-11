"""Host runner (ADR 0008): run a task's agent directly on the operator's machine, no container.

The Docker runner's shape without Docker. A task still gets a per-task ``git clone`` (ADR 0011),
still holds a ``/live`` connection, and still lives in a ``panopticon-<task_id>`` session on the
``-L panopticon`` tmux socket — so the dashboard, ``t`` attach, and the daemon's self-heal probes
work on a host task exactly as they do on a container one. What it drops is the image: there is no
base layer to build and no composed layer to compose, so the task's toolchain is **whatever the
operator's shell provides** (a repo's ``devenv``/``direnv``, the system PATH). That is the trade:
no build step and no runtime to install, in exchange for no isolation — see :meth:`spawn`.

Two processes share the pane, mirroring the container's PID 1 + ``docker exec`` pair: the
entrypoint (:mod:`panopticon.container.entrypoint`) is backgrounded to hold the liveness
connection, and :mod:`panopticon.container.host` runs the agent in the foreground so ``tmux
attach`` reaches a live ``claude``. The command executor is **injectable** so the runner is
unit-testable without tmux. LLM-free — the agent runs in the pane, not here.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from panopticon.core.dirs import secrets_file_path, user_data_dir
from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import (
    TMUX_SOCKET,
    CommandRunner,
    _subprocess_run,
    session_name,
)
from panopticon.sessionservice.runner import Runner

#: The liveness half: holds the ``/live`` connection open and sets the slug. Backgrounded in the
#: pane, so its death (with the pane) drops the registration exactly as a stopped container does.
LIVENESS_MODULE = "panopticon.container"

#: The agent half: the launcher, in the foreground so ``tmux attach`` reaches ``claude``. **Not**
#: ``panopticon.container.agent`` — that one ends by signalling PID 1, which on a host is init.
#: See :func:`panopticon.container.host._end_pane`.
AGENT_MODULE = "panopticon.container.host"

#: How a host task's agent answers permission prompts. The container backend skips them outright —
#: it is a throwaway box around a per-task clone, so there is nothing to protect and no operator to
#: ask. Here the agent runs **as the operator**, so skipping them would put the whole machine inside
#: the blast radius of an unattended prompt injection. ``auto`` keeps the agent moving on ordinary
#: work and stops it on the rest, which is the only honest default when the boundary is a judgement
#: call rather than a container. It is a classifier, not a sandbox: it narrows the blast radius, it
#: does not close it — the isolation caveat on :meth:`HostRunner.spawn` still stands in full.
DEFAULT_PERMISSION_MODE = "auto"


def task_home(task_id: str, *, homes_root: str | Path | None = None) -> str:
    """The task's private ``HOME`` — ``$PANOPTICON_DATA/homes/<task_id>`` by default.

    The host analogue of the container's per-task config volume (``CONFIG_MOUNT``), and load-bearing
    for two reasons. It keeps claude's session transcripts across a respawn, so ``R`` resumes the
    conversation rather than starting one; and it keeps the agent's generated config — skills,
    turn-flip hooks, MCP wiring, the pre-accepted trust dialog — **out of the operator's real
    ``~/.claude``**, which a task running as the operator would otherwise rewrite on every spawn.

    Deliberately not inside the workspace: the workspace is a git checkout, and a home directory
    written into it would show up as untracked files in every ``git status`` the agent runs.
    """
    root = Path(homes_root) if homes_root is not None else user_data_dir() / "homes"
    return str(root / task_id)


class HostRunner(Runner):
    """Runs a task's agent in a host tmux session on this machine (one host, no container)."""

    def __init__(
        self,
        service_url: str,
        *,
        runner_id: str = "local",
        tmux_socket: str | None = TMUX_SOCKET,
        python: str | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        secrets_dir: str | Path | None = None,
        homes_root: str | Path | None = None,
        run: CommandRunner = _subprocess_run,
        makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    ) -> None:
        self._service_url = service_url
        self._runner_id = runner_id
        self._tmux_socket = tmux_socket
        # The interpreter the task's two halves run under. Defaults to the one running the session
        # service, so a host task uses the same panopticon install as the daemon that spawned it
        # (a bare `python` would resolve against the operator's shell, which may be another venv).
        self._python = python or sys.executable
        # Passed to `claude --permission-mode`; see DEFAULT_PERMISSION_MODE for why it is not the
        # container's blanket skip. An operator who wants a stricter posture (`plan`) or a looser
        # one on a machine they treat as disposable can say so here.
        self._permission_mode = permission_mode
        # Root the repo's `env_file` name resolves against — this host's secrets dir (ADR 0007).
        self._secrets_dir = secrets_dir
        self._homes_root = homes_root
        self._run = run
        self._makedirs = makedirs

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        workspace: str | None = None,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        """Start the task's agent in a fresh host tmux session; return the session name.

        ``workspace`` is the task's per-task clone (ADR 0011) and becomes the pane's working
        directory — the agent's ``cwd``, exactly as ``/workspace`` is in a container. ``env_file``
        is the repo's secrets reference (ADR 0007), a name relative to **this** host's secrets dir;
        it is sourced into the pane *before* panopticon's own variables, so a stray ``PANOPTICON_*``
        in an operator's secrets file cannot displace the protocol's. ``initial_prompt``, ``turn``
        and ``starting_model`` are passed through the same ``PANOPTICON_*`` variables the container
        launcher already reads, so first-run prompting, respawn interruption and model selection
        behave identically. ``progress`` reports ``STARTING`` (before the session) then ``AWAITING``
        (once it is up); there is no ``BUILDING`` — a host task has no image.

        **This backend has no isolation, by construction.** The agent runs as the operator, with the
        operator's filesystem, credentials and network — it can reach every repo on the machine and
        the secrets dir itself. The container and Kubernetes backends put a boundary there; this one
        trades that boundary for a task that starts in seconds and uses the repo's own toolchain.
        Because of that it launches with ``--permission-mode`` (see :data:`DEFAULT_PERMISSION_MODE`)
        rather than the container's blanket skip — a narrower blast radius, not a closed one. Choose
        this backend per workflow (``runner_type = "host"``), knowingly.

        Idempotent: a stale session of the same name is killed first, so a respawn is a restart that
        resumes from the task's home (its claude history), not a second session.
        """

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        session = session_name(task_id)
        home = task_home(task_id, homes_root=self._homes_root)
        self._makedirs(home)
        start_dir = workspace or home
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            # The liveness registration is keyed on this the way a container is keyed on its name;
            # the session name keeps a host task addressable by the same identifier everywhere.
            "PANOPTICON_CONTAINER_ID": session,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            # The task's private config root — see `task_home`. Set last-wins over the env-file.
            "HOME": home,
            # The agent launcher turns this into `claude --permission-mode`; without it the
            # launcher falls back to the container's `--dangerously-skip-permissions`, which is
            # exactly the wrong default for a process running as the operator.
            "PANOPTICON_PERMISSION_MODE": self._permission_mode,
        }
        if initial_prompt:
            env["PANOPTICON_INITIAL_PROMPT"] = initial_prompt
        if turn:
            env["PANOPTICON_TASK_TURN"] = turn
        if starting_model:
            env["PANOPTICON_STARTING_MODEL"] = starting_model

        lines = []
        # Secrets first, panopticon's own variables after: `set -a` exports everything the file
        # assigns, so sourcing it later could overwrite the protocol's variables with an operator's.
        if env_path := secrets_file_path(env_file, secrets_dir=self._secrets_dir):
            quoted = shlex.quote(env_path)
            lines.append(f"export PANOPTICON_ENV_FILE={quoted}")
            lines.append(f"[ -f {quoted} ] && {{ set -a; . {quoted}; set +a; }}")
        lines += [f"export {key}={shlex.quote(value)}" for key, value in env.items()]
        python = shlex.quote(self._python)
        lines += [
            # The liveness half, backgrounded — the container's PID 1 without a container.
            f"{python} -m {LIVENESS_MODULE} >/dev/null 2>&1 &",
            "_panopticon_live_pid=$!",
            # Killing the session SIGHUPs the whole pane group and reaps it anyway; the trap covers
            # the other exit, where the agent returns and the shell ends on its own.
            "trap 'kill $_panopticon_live_pid 2>/dev/null' EXIT",
            # The agent half, in the foreground: this is what `tmux attach` reaches.
            f"exec {python} -m {AGENT_MODULE}",
        ]
        command = "\n".join(lines)
        # Clear a stale session first so a respawn is idempotent (no-op when none exists).
        self._run(self._tmux("kill-session", "-t", session), check=False)
        _report(LifecyclePhase.STARTING)
        self._run(
            self._tmux("new-session", "-d", "-s", session, "-c", start_dir, "sh", "-c", command)
        )
        _report(LifecyclePhase.AWAITING)  # session up; waiting for its /live registration
        return session

    def is_running(self, task_id: str) -> bool:
        """Whether the task's session is alive — the running signal for a host task.

        The session **is** the task: the pane holds both halves, so it lives exactly as long as the
        agent does. Mirrors the local runner's method name so the spawner can probe either backend
        uniformly."""
        return self.has_session(task_id)

    def has_session(self, task_id: str) -> bool:
        """Whether the task's host tmux session exists on this runner's tmux server."""
        session = session_name(task_id)
        sessions = self._run(self._tmux("list-sessions", "-F", "#{session_name}"), check=False)
        return session in sessions.splitlines()

    def delete_workspace_contents(self, path: str) -> None:
        """Delete everything inside ``path``.

        The host counterpart of the Docker runner's throwaway-root-container trick, which exists
        only because a container can leave root-owned files behind. A host task runs as the
        operator, so nothing it wrote needs privilege to remove and a plain walk suffices — and
        crucially this keeps workspace cleanup working on a machine with no Docker at all."""
        root = Path(path)
        if not root.is_dir():
            return
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                _rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def delete_home(self, task_id: str) -> None:
        """Remove the task's private ``HOME`` (its claude history and generated config).

        Called when a terminal task's workspace is cleaned up. The home is deliberately *outside*
        the workspace (see :func:`task_home`), so the workspace cleanup cannot reach it and it would
        otherwise accumulate one directory per task forever. Idempotent — a task that never ran on
        this backend has none."""
        home = Path(task_home(task_id, homes_root=self._homes_root))
        if home.is_dir():
            _rmtree(home)

    def stop(self, session_id: str) -> None:
        """Kill the task's tmux session. Idempotent — tolerates an already-gone session.

        Killing the session SIGHUPs the pane's process group, so the agent and the backgrounded
        liveness process both die and the registration drops."""
        self._run(self._tmux("kill-session", "-t", session_id), check=False)


def _rmtree(path: Path) -> None:
    """Recursively remove ``path`` (a directory), depth first."""
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink(missing_ok=True)
    os.rmdir(path)
