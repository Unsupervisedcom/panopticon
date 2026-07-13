"""Host shell-script runner: run a workflow's ``shell_script`` in a host tmux session, no container.

The companion to :class:`~panopticon.sessionservice.local_runner.LocalRunner` for workflows whose
:attr:`~panopticon.core.workflow.Workflow.runner_type` is ``"shell"``. Where the local runner
spawns a Docker container + a tmux pane execing into it, this runner just opens a **host tmux
session** whose single pane runs the workflow's script directly on the host — no image, no
per-task clone, no in-container agent. It's for short operator utilities that need a real shell and
TTY (e.g. ``claude setup-token``, whose interactive OAuth flow the operator completes by attaching
to the session).

It shares the local runner's tmux socket (``-L panopticon``) and its ``panopticon-<task_id>``
session naming, so the terminal supervisor's ``t`` (attach to a task's session) reaches a shell
task exactly as it does a container one. The command executor is **injectable** so the runner is
unit-testable without tmux. LLM-free — a shell task runs no agent.
"""

from __future__ import annotations

import importlib.resources
import os
import shlex
from collections.abc import Callable
from pathlib import Path

from panopticon.core.dirs import secrets_file_path
from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import (
    TMUX_SOCKET,
    CommandRunner,
    _subprocess_run,
    session_name,
)
from panopticon.sessionservice.runner import Runner

#: The panopticon shell lib (``task_lib.sh``): functions a shell workflow's script uses to drive its
#: task over REST (``panopticon_advance``/``_drop``/``_set_slug``/…) instead of hand-rolling curl.
#: Loaded once at import and injected into every shell task's shell (see :meth:`ShellRunner.spawn`).
_TASK_LIB = (importlib.resources.files("panopticon.sessionservice") / "task_lib.sh").read_text()


class ShellRunner(Runner):
    """Runs a shell workflow's script in a host tmux session (one host, no container)."""

    def __init__(
        self,
        service_url: str,
        *,
        runner_id: str = "local",
        tmux_socket: str | None = TMUX_SOCKET,
        secrets_dir: str | Path | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._runner_id = runner_id
        self._tmux_socket = tmux_socket
        # Root the repo's `env_file` *name* resolves against — this host's local secrets dir, matching
        # LocalRunner (ADR 0007). None = resolve the host's secrets dir dynamically at spawn.
        self._secrets_dir = secrets_dir
        self._run = run

    def _tmux(self, *args: str) -> list[str]:
        prefix = ["tmux", *(["-L", self._tmux_socket] if self._tmux_socket else [])]
        return [*prefix, *args]

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        script: str = "",
        workdir: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        """Run ``script`` for ``task_id`` in a fresh host tmux session; return the session name.

        The session is named ``panopticon-<task_id>`` (matching the local runner) so the terminal
        supervisor attaches to it the same way, and starts in ``workdir`` — the task's own directory
        the spawner prepares (empty by default, or a repo clone when the workflow opts in), or the
        workflow's own override. ``workdir`` falls back to the operator's home only when unset (direct
        use). The pane runs ``sh -c`` with ``PANOPTICON_SERVICE_URL`` and ``PANOPTICON_TASK_ID``
        exported — so the script can drive its own lifecycle over REST (e.g. advance to COMPLETE on
        success) — and the repo's ``env_file`` secrets sourced first when given. ``env_file`` is a
        **name relative to this runner's secrets dir** (ADR 0007), resolved host-locally (like
        ``LocalRunner``) so a remote runner uses its own host's secrets. The panopticon shell lib
        (``panopticon_advance``/``_drop``/…) is loaded into the shell so the script can drive its task
        over REST. It also holds a ``/live`` registration open in the background for the session's
        lifetime, so the dashboard shows the task **live** (not ``awaiting``) while the script runs. Reports ``STARTING`` (before the
        session) then ``AWAITING`` (once it's up) via ``progress`` — it composes to ``live`` once the
        background registration connects; there is no ``PREPARING``/``BUILDING`` (no clone, no image).
        Idempotent: a stale session of the same name is killed first, so a respawn is a no-op restart."""

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        start_dir = workdir or os.path.expanduser("~")
        session = session_name(task_id)
        # A shell task runs no agent to open its own `/live` registration, so the dashboard would
        # read it as `awaiting` for its whole life. Hold the liveness stream open in the background
        # for the session's lifetime instead — the task then composes as `live` while the script
        # runs. `curl --no-buffer` keeps the GET open (the connection *is* the signal); `trap … EXIT`
        # drops it when the script exits, and killing the session SIGHUPs the whole pane group, which
        # reaps the backgrounded curl too — either way liveness ends exactly with the session.
        live_url = (
            f"{self._service_url}/tasks/{task_id}/live"
            f"?container_id={session}&runner_id={self._runner_id}"
        )
        lines = [
            f"export PANOPTICON_SERVICE_URL={shlex.quote(self._service_url)}",
            f"export PANOPTICON_TASK_ID={shlex.quote(task_id)}",
            f"export PANOPTICON_RUNNER_ID={shlex.quote(self._runner_id)}",
            f"curl --silent --no-buffer {shlex.quote(live_url)} >/dev/null 2>&1 &",
            "_panopticon_live_pid=$!",
            "trap 'kill $_panopticon_live_pid 2>/dev/null' EXIT",
            # Load the panopticon shell lib so the script can drive its task (panopticon_advance, …).
            _TASK_LIB,
        ]
        # Resolve the env_file *name* to an absolute path under this host's secrets dir, expose the
        # path (so a script can tell the operator where to add their own credential), then source it
        # if it exists (a not-yet-created secrets file is fine — the script sees the vars unset).
        if env_path := secrets_file_path(env_file, secrets_dir=self._secrets_dir):
            quoted = shlex.quote(env_path)
            lines.append(f"export PANOPTICON_ENV_FILE={quoted}")
            lines.append(f"[ -f {quoted} ] && {{ set -a; . {quoted}; set +a; }}")
        lines.append(script)
        command = "\n".join(lines)
        # Clear any stale session first so a respawn is idempotent (no-op when none exists).
        self._run(self._tmux("kill-session", "-t", session), check=False)
        _report(LifecyclePhase.STARTING)
        # -c sets the pane's start directory (the task's own dir) so the script runs in a known place.
        self._run(
            self._tmux("new-session", "-d", "-s", session, "-c", start_dir, "sh", "-c", command)
        )
        _report(LifecyclePhase.AWAITING)
        return session

    def is_running(self, task_id: str) -> bool:
        """Whether the task's shell session is alive — the running signal for a shell task.

        A shell task registers no ``/live`` connection (it runs no agent), so its tmux session
        **is** its liveness: the session lives exactly as long as the script's process. Mirrors the
        local runner's method name so the spawner can probe either backend uniformly."""
        return self.has_session(task_id)

    def has_session(self, task_id: str) -> bool:
        """Whether the task's host tmux session exists on this runner's tmux server."""
        session = session_name(task_id)
        sessions = self._run(self._tmux("list-sessions", "-F", "#{session_name}"), check=False)
        return session in sessions.splitlines()

    def stop(self, session_id: str) -> None:
        # Idempotent: tolerate an already-gone session.
        self._run(self._tmux("kill-session", "-t", session_id), check=False)
