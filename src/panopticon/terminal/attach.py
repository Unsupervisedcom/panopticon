"""Handing the terminal over to a task's tmux session (the `t` keybinding).

Mirrors cloude-cade: inside tmux, `switch-client`; otherwise `attach` (the caller suspends the
TUI first). Sessions live on the runner's dedicated `panopticon` tmux socket.
"""

from __future__ import annotations


def attach_command(session: str, *, socket: str, inside_tmux: bool) -> list[str]:
    """The tmux argv to reach ``session``: switch the current client, or attach a new one."""
    verb = "switch-client" if inside_tmux else "attach"
    return ["tmux", "-L", socket, verb, "-t", session]
