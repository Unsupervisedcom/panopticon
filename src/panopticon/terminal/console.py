"""The terminal session supervisor (ADR 0009 §6): owns the TTY and routes the operator.

Hub-and-spoke. The **dashboard runs in its own tmux session** (`dashboard`, on the panopticon
socket) alongside the task sessions, so the whole console is one tmux server. The supervisor
loop is::

    while (session := show_dashboard()) is not None:
        attach(session)

``show_dashboard`` attaches the (persistent) dashboard session and returns the task the operator
picked with `t` (or ``None`` when they quit/detach); ``attach`` hands the terminal to that task's
session until they detach (``C-b d``), then the loop re-attaches the **same, still-running**
dashboard — cursor and all.

The dashboard reports a pick by writing it to a **switch-file** and then detaching its client
(:func:`switch_to`): it stays alive in the background while the operator looks at the task, so
returning lands on the same dashboard. Switching is always detach→attach, never `switch-client`,
so a remote task is reached by the same loop at M5 — only the attach gains an ``ssh -t <host>``
prefix. LLM-free.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from panopticon.sessionservice.local_runner import TMUX_SOCKET
from panopticon.terminal.attach import attach_command

#: tmux session name the dashboard runs in (on the panopticon socket, beside the task sessions).
DASHBOARD_SESSION = "dashboard"

#: Show the dashboard and return the task session the operator picked, or ``None`` to quit.
Selector = Callable[[], "str | None"]
#: Hand the terminal to a task's session; blocks until the operator detaches.
Attacher = Callable[[str], None]


def _tmux_detach() -> None:
    subprocess.run(["tmux", "detach-client"], check=False)


def switch_to(
    session: str, *, switch_file: Path, detach: Callable[[], None] = _tmux_detach
) -> None:
    """The dashboard's `t` hook, run inside its tmux session: record the picked ``session`` for
    the supervisor, then detach this client so the supervisor attaches the task. The dashboard
    process keeps running (detached), so returning to it shows the same live view."""
    switch_file.write_text(session)
    detach()


def run_console(*, show_dashboard: Selector, attach: Attacher) -> None:
    """Loop: dashboard → (pick a task) → attach → (detach) → dashboard, until the operator quits.

    ``show_dashboard`` and ``attach`` are injected so the loop is testable without tmux or a TTY.
    """
    while (session := show_dashboard()) is not None:
        attach(session)


def run_console_local(service_url: str, *, socket: str = TMUX_SOCKET) -> None:
    """Wire :func:`run_console` to local tmux: a persistent `dashboard` session, and the task
    attach on the panopticon socket. The dashboard reports its pick via a switch-file."""
    switch_file = Path(tempfile.mkdtemp(prefix="panopticon-console-")) / "switch"
    dashboard = [
        sys.executable, "-m", "panopticon.terminal",
        "--service-url", service_url,
        "dashboard", "--switch-file", str(switch_file),
    ]

    def _tmux(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["tmux", "-L", socket, *args], check=False)

    def show_dashboard() -> str | None:
        switch_file.write_text("")  # clear last round's pick
        if _tmux("has-session", "-t", DASHBOARD_SESSION).returncode != 0:
            _tmux("new-session", "-d", "-s", DASHBOARD_SESSION, *dashboard)  # start it once, detached
        _tmux("attach", "-t", DASHBOARD_SESSION)  # blocks until `t` detaches (or `q` ends it)
        return switch_file.read_text().strip() or None

    def attach(session: str) -> None:
        subprocess.run(attach_command(session, socket=socket), check=False)

    run_console(show_dashboard=show_dashboard, attach=attach)
