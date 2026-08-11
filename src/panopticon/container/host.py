"""``python -m panopticon.container.host`` — the agent launcher for a task running on the host.

The third shape of one protocol. A task's agent needs the same two things wherever it runs: a
**liveness connection** held open beside it, and the **agent launcher** in the pane so ``tmux
attach`` reaches a live ``claude``. :mod:`panopticon.container.entrypoint` and
:mod:`panopticon.container.agent` already provide both, and neither imports Docker or Kubernetes —
they speak HTTP to the task service and read ``PANOPTICON_*`` from the environment. A container
composes them as PID 1 plus a ``docker exec``; a pod composes them in
:mod:`panopticon.container.pod`; a host task composes them as a backgrounded process plus this
module, in one tmux pane.

Exactly one thing in the launcher cannot be reused as-is, and it is the reason this module exists
rather than the runner calling ``panopticon.container.agent`` directly — see :func:`_end_pane`.
"""

from __future__ import annotations

from panopticon.container import agent


def _end_pane() -> None:
    """The host's replacement for the container launcher's "stop the container" step.

    :func:`panopticon.container.agent.main` ends by calling ``on_exit`` so a finished agent takes
    its container down (task → ``down`` → the operator respawns with ``R``). In a container that is
    ``os.kill(1, SIGTERM)``: PID 1 is the entrypoint holding the liveness connection. **On a host,
    PID 1 is the operator's init**, so that call must never run here — hence this override, and
    hence the runner launches this module and never ``panopticon.container.agent``.

    Nothing has to be signalled in its place. This process *is* the tmux pane's command, so
    returning ends the pane; tmux then ends the session and SIGHUPs the pane's process group, which
    reaps the liveness process the runner backgrounded beside us. The registration drops and the
    task shows ``down`` — the same observable end-state as a stopped container, reached by exiting
    rather than by signalling.
    """


def main() -> None:
    """Run the agent launcher with a host-safe exit.

    Everything else is the container's behaviour unchanged: the CLI config dir is ``$HOME/.claude``
    (the runner points ``HOME`` at the task's own directory, so the operator's real ``~/.claude`` is
    never touched), and auth is the ``CLAUDE_CODE_OAUTH_TOKEN`` the runner sourced from the repo's
    ``env_file``.
    """
    agent.main(on_exit=_end_pane)


if __name__ == "__main__":  # pragma: no cover
    main()
