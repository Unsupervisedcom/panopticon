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
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import TMUX_SOCKET
from panopticon.terminal.attach import attach_command

#: tmux session name the dashboard runs in (on the panopticon socket, beside the task sessions).
DASHBOARD_SESSION = "dashboard"
#: tmux session name the task service runs in under `make start` (beside the dashboard).
SERVICE_SESSION = "service"
#: tmux session name the session-service runner runs in under `make start`.
RUNNER_SESSION = "runner"


def switch_file_path(socket: str) -> Path:
    """The supervisor↔dashboard switch-file, **deterministic per socket**.

    The `dashboard` tmux session outlives any one supervisor (it's reused across `make start`
    invocations via ``has-session``), so the path the dashboard writes its `t` pick to must not be
    per-invocation. A fresh temp path each run desyncs them: a re-invoked supervisor reads a *new*
    empty file while the still-running dashboard writes picks to the *old* one — so every `t` reads
    as empty (a quit), detaching the operator to the shell instead of attaching the task. Keying the
    path to the socket keeps a re-attached dashboard and its supervisor on the same file.
    """
    return Path(tempfile.gettempdir()) / f"panopticon-console-{socket}" / "switch"


#: Show the dashboard and return the task session the operator picked, or ``None`` to quit.
Selector = Callable[[], "str | None"]
#: Hand the terminal to a task's session; blocks until the operator detaches.
Attacher = Callable[[str], None]


def _tmux_detach() -> None:
    subprocess.run(["tmux", "detach-client"], check=False)


def encode_switch_target(session: str, host: str | None) -> str:
    """Encode a ``(session, host)`` pick into a switch-file line: ``"<host>\\t<session>"`` for a
    remote runner (so the supervisor can ssh-wrap the attach), bare ``"<session>"`` when local.

    The **one** place the switch-file format is written — the `t` hook (:func:`switch_to`) and the
    `panopticon start <task>` join (:func:`resolve_join`) both go through it; :func:`decode_switch_target`
    is the inverse the supervisor's attach parses with."""
    return f"{host}\t{session}" if host else session


def decode_switch_target(line: str) -> tuple[str, str | None]:
    """Inverse of :func:`encode_switch_target`: the ``(session, host)`` from a switch-file line
    (``host`` is ``None`` when there's no ``\\t`` — a local pick)."""
    parts = line.split("\t", 1)
    host = parts[0] if len(parts) == 2 else None
    return parts[-1], host or None


def switch_to(
    session: str,
    *,
    host: str | None = None,
    switch_file: Path,
    detach: Callable[[], None] = _tmux_detach,
) -> None:
    """The dashboard's `t` hook, run inside its tmux session: record the picked ``session`` for
    the supervisor, then detach this client so the supervisor attaches the task. The dashboard
    process keeps running (detached), so returning to it shows the same live view.

    When ``host`` is set the switch-file carries ``<host>\\t<session>`` so the
    supervisor can ssh-wrap the attach; a plain ``<session>`` (no tab) means local.
    """
    switch_file.write_text(encode_switch_target(session, host))
    detach()


def session_exists(session: str, *, socket: str = TMUX_SOCKET) -> bool:
    """Whether the named tmux session is running on the panopticon socket."""
    return (
        subprocess.run(
            ["tmux", "-L", socket, "has-session", "-t", session], capture_output=True
        ).returncode
        == 0
    )


def make_session_switch(
    session: str,
    switch_file: Path,
    *,
    socket: str = TMUX_SOCKET,
    exists: Callable[[], bool] | None = None,
    detach: Callable[[], None] = _tmux_detach,
) -> Callable[[], bool]:
    """Build a dashboard sibling-session hook: switch to ``session`` **when it exists**, returning
    whether it did. Like the `t` hook it records the pick + detaches (:func:`switch_to`); with no
    such session it does nothing (no detach), so the dashboard can report it."""
    is_running = exists or (lambda: session_exists(session, socket=socket))

    def switch() -> bool:
        if not is_running():
            return False
        switch_to(session, switch_file=switch_file, detach=detach)
        return True

    return switch


def make_service_switch(
    switch_file: Path,
    *,
    socket: str = TMUX_SOCKET,
    exists: Callable[[], bool] | None = None,
    detach: Callable[[], None] = _tmux_detach,
) -> Callable[[], bool]:
    """Build the dashboard's `s` hook: switch to the task-service session when one exists."""
    return make_session_switch(
        SERVICE_SESSION, switch_file, socket=socket, exists=exists, detach=detach
    )


def make_runner_switch(
    switch_file: Path,
    *,
    socket: str = TMUX_SOCKET,
    exists: Callable[[], bool] | None = None,
    detach: Callable[[], None] = _tmux_detach,
) -> Callable[[], bool]:
    """Build the dashboard's `u` hook: switch to the session-service (runner) session when one exists."""
    return make_session_switch(
        RUNNER_SESSION, switch_file, socket=socket, exists=exists, detach=detach
    )


def _service_ready(service_url: str) -> bool:
    """Whether the task service answers its health check (gates the dashboard on startup)."""
    try:
        return httpx.get(f"{service_url.rstrip('/')}/healthz", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def wait_for_service(
    service_url: str,
    *,
    ready: Callable[[str], bool] = _service_ready,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 150,
    interval: float = 0.2,
) -> bool:
    """Poll the task service until it answers, returning whether it came up within ``attempts``.

    `make start` starts the service, runner, and console near-simultaneously; without this the
    console would start the dashboard before the service is listening, the dashboard would crash on
    its first REST read, and its tmux session would vanish ("can't find session: dashboard")."""
    for _ in range(attempts):
        if ready(service_url):
            return True
        sleep(interval)
    return False


def resolve_join(
    client: TaskServiceClient,
    ref: str,
    *,
    attempts: int = 1,
    interval: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Resolve a task ``ref`` (id or slug) to the supervisor switch-file target for its live
    container session, or ``None`` when there's no such task / no running container.

    Mirrors the dashboard's `t` hook — the session name is the container id, paired with the task's
    ``runner_host`` and run through :func:`encode_switch_target`. Used to *join* a task directly on
    `panopticon start <task>`.

    Registrations are connection-scoped in-memory liveness, so a just-(re)started task service holds
    none until each container reconnects its /live stream. ``attempts``/``interval`` poll across that
    reconnect window (the join races it) before giving up; an *unknown* task returns immediately —
    waiting won't conjure it.
    """
    for attempt in range(attempts):
        match = next(
            (t for t in client.list_tasks() if t.get("id") == ref or t.get("slug") == ref), None
        )
        if match is None:
            return None
        registrations = client.list_registrations(str(match["id"]))
        if registrations:
            session = str(registrations[0]["container_id"])
            return encode_switch_target(session, match.get("runner_host"))
        if attempt < attempts - 1:
            sleep(interval)
    return None


def run_console(*, show_dashboard: Selector, attach: Attacher, initial: str | None = None) -> None:
    """Loop: dashboard → (pick a task) → attach → (detach) → dashboard, until the operator quits.

    ``initial`` (set by `panopticon start <task>`) is attached once up front, before the first
    dashboard, so the operator lands straight in that task's session; detaching falls into the
    normal loop. ``show_dashboard`` and ``attach`` are injected so the loop is testable without
    tmux or a TTY.
    """
    if initial is not None:
        attach(initial)
    while (session := show_dashboard()) is not None:
        attach(session)


def run_console_local(
    service_url: str,
    *,
    socket: str = TMUX_SOCKET,
    client: TaskServiceClient | None = None,
    join: str | None = None,
) -> None:
    """Wire :func:`run_console` to local tmux: a persistent `dashboard` session, and the task
    attach on the panopticon socket. The dashboard reports its pick via a switch-file.

    ``join`` (a task id or slug from `panopticon start <task>`) is resolved to its live container
    session and attached first; if the task or its container isn't found we fall back to the
    dashboard with a notice rather than blocking."""
    # Don't show the dashboard until the service is up, else it crashes on its first read (and its
    # session vanishes) — the `make start` startup race.
    if not wait_for_service(service_url):
        print(f"task service not reachable at {service_url}; is it running?", file=sys.stderr)
        return
    initial: str | None = None
    if join:
        client = client or TaskServiceClient(httpx.Client(base_url=service_url))
        # Poll across the container's /live reconnect window: `start`/`quickstart` may have just
        # (re)started the runner, and a freshly created task (quickstart's setup-repo) is only
        # claimed + spawned a beat later — resolve on the first hit, ~10s ceiling.
        initial = resolve_join(client, join, attempts=50, interval=0.2)
        if initial is None:
            print(f"no running container for task '{join}'; opening the dashboard", file=sys.stderr)
    switch_file = switch_file_path(socket)
    switch_file.parent.mkdir(parents=True, exist_ok=True)
    dashboard = [
        sys.executable,
        "-m",
        "panopticon.terminal",
        "--service-url",
        service_url,
        "dashboard",
        "--switch-file",
        str(switch_file),
    ]

    def _tmux(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["tmux", "-L", socket, *args], check=False)

    def show_dashboard() -> str | None:
        switch_file.write_text("")  # clear last round's pick
        if _tmux("has-session", "-t", DASHBOARD_SESSION).returncode != 0:
            _tmux(
                "new-session", "-d", "-s", DASHBOARD_SESSION, *dashboard
            )  # start it once, detached
        _tmux("attach", "-t", DASHBOARD_SESSION)  # blocks until `t` detaches (or `q` ends it)
        return switch_file.read_text().strip() or None

    def attach(pick: str) -> None:
        session, host = decode_switch_target(pick)
        subprocess.run(attach_command(session, socket=socket, host=host), check=False)

    run_console(show_dashboard=show_dashboard, attach=attach, initial=initial)
