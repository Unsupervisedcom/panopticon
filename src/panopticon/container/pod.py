"""The pod bootstrap: what a Kubernetes task runs instead of the Docker entrypoint (ADR 0014).

A Docker task leans on its host — the session service clones the per-task checkout and mounts it,
and the tmux session the operator attaches to lives on the host, with the pane execing into the
container. A task Job has no such host, so this module does both jobs from inside the pod:

1. **clone the workspace** from the repo's git URL into ``/workspace`` (an ``emptyDir``), the
   writable checkout the agent works in for the whole task;
2. **start the agent** in an *in-pod* tmux session named like the Job, so
   ``kubectl exec -it <pod> -- tmux attach -t panopticon-<task_id>`` reaches the live agent;
3. **hold the liveness connection** by handing off to the ordinary container entrypoint — the same
   protocol, and therefore the same task-service view, as a Docker task — but only for as long as
   that tmux session lives, so a dead agent reads as a down task rather than a live one nobody is
   home in (see :func:`main`).

Everything before the handoff is deterministic setup: this module makes no LLM call. The agent it
starts does, in the tmux session, exactly as on the Docker path.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from panopticon.container.entrypoint import _until_signalled
from panopticon.container.entrypoint import main as serve_liveness

#: The in-pod tmux session's pane command: the agent launcher (it renders the workflow's skills and
#: operations, points ``claude`` at the task service's MCP server, then execs it).
AGENT_COMMAND: tuple[str, ...] = ("python", "-m", "panopticon.container.agent")


class CommandRunner(Protocol):
    """Runs ``git``/``tmux`` in the pod. Injectable so the bootstrap is testable without either."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> None: ...


class SessionProbe(Protocol):
    """Answers whether a tmux session exists. Separate from :class:`CommandRunner` because this one
    reports a **result** rather than running a command that must succeed."""

    def __call__(self, session: str) -> bool: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> None:
    subprocess.run(list(args), check=check)


def _tmux_has_session(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session], check=False).returncode == 0


def clone_workspace(
    git_url: str,
    workspace: Path,
    *,
    run: CommandRunner = _subprocess_run,
) -> bool:
    """Clone ``git_url`` into ``workspace``; return whether a clone actually ran.

    A no-op when the directory already holds a checkout, which is what makes a **respawn** safe: the
    Job is recreated with the same name, and on the rare occasion its pod restarts onto a populated
    volume the task resumes rather than re-cloning over its own work.
    """
    if (workspace / ".git").exists():
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", git_url, str(workspace)], check=True)
    return True


def start_agent_session(
    session: str,
    workspace: Path,
    *,
    agent_command: Sequence[str] = AGENT_COMMAND,
    run: CommandRunner = _subprocess_run,
) -> None:
    """Start the agent in a detached in-pod tmux session named ``session``, rooted at ``workspace``.

    Detached (``-d``) because nothing is attached yet — the operator attaches later, over
    ``kubectl exec``, and finds the agent already working.
    """
    run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(workspace), *agent_command],
        check=True,
    )


def bootstrap(
    env: Mapping[str, str],
    *,
    run: CommandRunner = _subprocess_run,
) -> None:
    """Prepare the pod: clone the workspace (when a git URL is given) and start the agent session.

    A task with no ``PANOPTICON_GIT_URL`` gets an empty workspace rather than an error — the agent
    still starts, and an operator attaching sees a working task they can direct, which is a better
    failure than a pod that exits before anything is attachable.
    """
    workspace = Path(env.get("PANOPTICON_WORKSPACE", "/workspace"))
    if git_url := env.get("PANOPTICON_GIT_URL"):
        clone_workspace(git_url, workspace, run=run)
    start_agent_session(env["PANOPTICON_CONTAINER_ID"], workspace, run=run)


def session_alive(session: str, *, probe: SessionProbe = _tmux_has_session) -> bool:
    """Whether the in-pod tmux session still exists — i.e. whether the agent is still there."""
    return probe(session)


def main() -> None:
    """``python -m panopticon.container.pod`` — bootstrap, then hold liveness while the agent lives.

    Liveness is held only **while the tmux session exists**. On the Docker path a dead agent takes
    its host tmux session with it, and the host daemon reads that missing session as a task to heal.
    A pod has no host-side session to lose: this process would happily go on holding the connection
    with nothing running behind it, and the task would read **live** while being unattachable. So the
    session is the liveness condition here — when it ends, the connection closes, the task reads
    down, and the daemon respawns it exactly as it would a dead container.
    """
    env = os.environ
    bootstrap(env)
    session = env["PANOPTICON_CONTAINER_ID"]
    signalled = _until_signalled()
    serve_liveness(running=lambda: signalled() and session_alive(session))


if __name__ == "__main__":  # pragma: no cover
    main()
