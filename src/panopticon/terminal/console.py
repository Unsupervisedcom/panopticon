"""The terminal session supervisor (ADR 0009): owns the TTY and routes the operator.

A hub-and-spoke loop — show the dashboard; when the operator picks a task (`t`), the dashboard
exits handing back that task's tmux session; the supervisor **attaches** the terminal to it;
when the operator detaches (tmux's detach key, ``C-b d`` by default), control returns here and
the dashboard is shown again. Quitting the dashboard (`q`) ends the loop.

Switching is always detach→attach, never `switch-client`. That is what lets a remote task be
reached by the same loop at M5: only :func:`panopticon.terminal.attach.attach_command` changes
(an ``ssh -t <host>`` prefix), not this supervisor. LLM-free — a REST client of the task service.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import TMUX_SOCKET
from panopticon.terminal.attach import attach_command
from panopticon.terminal.dashboard import run as run_dashboard

#: Pick the next task session to attach to (the dashboard), or ``None`` to quit.
Selector = Callable[[TaskServiceClient], "str | None"]
#: Hand the terminal to a task's session; blocks until the operator detaches.
Attacher = Callable[[str], None]


def _attach(session: str) -> None:
    """Attach the terminal to ``session`` on the panopticon tmux socket; blocks until detach."""
    subprocess.run(attach_command(session, socket=TMUX_SOCKET), check=False)


def run_console(
    client: TaskServiceClient,
    *,
    select: Selector = run_dashboard,
    attach: Attacher = _attach,
) -> None:
    """Loop: dashboard → (pick a task) → attach → (detach) → dashboard, until the operator quits.

    ``select`` and ``attach`` are injectable so the loop is testable without a TTY or tmux.
    """
    while (session := select(client)) is not None:
        attach(session)
