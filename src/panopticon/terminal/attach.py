"""Building the command to hand the terminal to a task's tmux session.

The terminal controller is a *supervisor* that owns the TTY (ADR 0009): it shows the dashboard,
and on `t` it leaves the dashboard and **attaches** the terminal to the chosen task's tmux
session, rejoining the dashboard when the operator detaches. Switching is therefore always
detach→attach — never `switch-client` — which is what lets a future remote task be reached the
same way, by prefixing the attach with ``ssh -t <host>`` (ADR 0009 §6). Sessions live on the
runner's dedicated `panopticon` tmux socket.
"""

from __future__ import annotations


def attach_command(session: str, *, socket: str, host: str | None = None) -> list[str]:
    """The argv that attaches the current terminal to ``session`` on the panopticon socket.

    ``host`` is reserved for remote runners (M5): when set, the attach is wrapped in
    ``ssh -t <host> …`` so the same supervisor loop reaches a session on another machine.
    """
    tmux = ["tmux", "-L", socket, "attach", "-t", session]
    return ["ssh", "-t", host, *tmux] if host else tmux
