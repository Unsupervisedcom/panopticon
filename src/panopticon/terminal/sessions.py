"""The background control-plane tmux sessions — starting them, and restarting them in place.

`panopticon start` runs the task service and the session-service runner as detached sessions on
the panopticon tmux socket (beside the `dashboard` session and every ``panopticon-<task-id>`` task
session). This module owns **the one launch table** those sessions come from, so `start` and
`restart` can't drift: :func:`background_sessions` (service + runner) plus
:func:`~panopticon.terminal.console.dashboard_command`, consumed by :func:`start_sessions` and
:func:`restart_sessions`.

:func:`restart_sessions` is the in-place bounce behind `panopticon restart` / `make restart`.
``make stop`` + ``make start`` is otherwise the only restart path, and ``stop`` tears down every
task container and the whole tmux server. Restarting *just* these daemons is safe: task sessions
are siblings on the same server and are never touched, containers re-register with the new task
service over their ``/live`` heartbeat, and the runner comes back under the same runner id so its
claims stay valid (``Spawner.startup_reclaim`` releases a claim only when the container is really
gone). LLM-free, and it never shells out to ``docker``.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TextIO

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.models import ContainerStatus
from panopticon.sessionservice.local_runner import TMUX_SOCKET, session_name
from panopticon.terminal.console import (
    DASHBOARD_SESSION,
    RUNNER_SESSION,
    SERVICE_SESSION,
    dashboard_command,
    service_ready,
    switch_file_path,
)

#: The prefix every task's tmux session carries (``session_name("")``) — what the before/after
#: summary counts, and the sessions a restart must never touch.
TASK_SESSION_PREFIX = session_name("")

#: Restart targets, in the order they're restarted: the task service first (the runner and the
#: dashboard both talk to it), then the runner, then the dashboard.
TARGETS = (SERVICE_SESSION, RUNNER_SESSION, DASHBOARD_SESSION)
#: What `panopticon restart` bounces with no argument. The dashboard is the operator's foreground
#: — restarting it drops an attached supervisor back to the shell — so it's opt-in.
DEFAULT_TARGETS = (SERVICE_SESSION, RUNNER_SESSION)

#: Bounded-wait budgets (attempts × interval seconds), so a wedged process fails loudly instead of
#: hanging the operator's terminal.
STOP_ATTEMPTS = 100  #: 20s for the old process to exit and free its port
STOP_INTERVAL = 0.2
READY_ATTEMPTS = 150  #: 30s for the relaunched process to answer
READY_INTERVAL = 0.2
SETTLE_ATTEMPTS = 150  #: 30s for the task containers to re-register with the new task service
SETTLE_INTERVAL = 0.2


def background_command(module: str, log: str) -> str:
    """The shell command a background session runs: ``<python> -m <module>``, teed to ``log``.

    The interpreter path is shell-quoted: a pipx install on macOS lives under
    ``~/Library/Application Support/...``, whose space would otherwise word-split when tmux runs
    the command through ``/bin/sh -c`` and fail with "no such file or directory: …/Application".
    """
    return f"{shlex.quote(sys.executable)} -m {module} 2>&1 | tee {log}"


def background_sessions() -> list[tuple[str, str]]:
    """The ``(session, command)`` table for the background control-plane sessions, in start order —
    the single source both `start` and `restart` launch from.

    A function rather than a constant: ``sys.executable`` is read at call time.
    """
    return [
        (
            SERVICE_SESSION,
            background_command("panopticon.taskservice", "/tmp/panopticon-service.log"),
        ),
        (
            RUNNER_SESSION,
            background_command("panopticon.sessionservice.host", "/tmp/panopticon-runner.log"),
        ),
    ]


#: How commands are run — injected so the emitted argv can be pinned in tests without tmux.
CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=check)


def _tmux(*args: str, socket: str = TMUX_SOCKET) -> list[str]:
    return ["tmux", "-L", socket, *args]


def _has_session(name: str, *, run: CommandRunner, socket: str) -> bool:
    return run(_tmux("has-session", "-t", name, socket=socket)).returncode == 0


def start_sessions(*, run: CommandRunner = _run, socket: str = TMUX_SOCKET) -> None:
    """Start whichever background control-plane sessions aren't already running.

    Don't bounce an already-running session. Restarting the task service wipes its in-memory
    registrations (connection-scoped liveness), so a `panopticon start <task>` that restarted a
    healthy service would find no container to join until every task reconnects its /live stream —
    the join races the reconnect and falls back to the dashboard. Leave it be; `panopticon restart`
    is the deliberate bounce.
    """
    for name, cmd in background_sessions():
        if _has_session(name, run=run, socket=socket):
            continue
        run(_tmux("new-session", "-d", "-s", name, cmd, socket=socket), check=True)


def resolve_targets(targets: Sequence[str]) -> list[str]:
    """The sessions to restart, always in restart order: nothing → service + runner, ``all`` →
    every target (the dashboard included)."""
    if not targets:
        return list(DEFAULT_TARGETS)
    if "all" in targets:
        return list(TARGETS)
    return [name for name in TARGETS if name in targets]  # de-duplicated, in restart order


def task_session_count(*, run: CommandRunner = _run, socket: str = TMUX_SOCKET) -> int:
    """How many ``panopticon-<task-id>`` task sessions are on the socket — a read-only count for
    the before/after summary (the restart itself never touches them)."""
    result = run(_tmux("list-sessions", "-F", "#{session_name}", socket=socket))
    return sum(1 for line in result.stdout.splitlines() if line.startswith(TASK_SESSION_PREFIX))


def live_task_count(client: TaskServiceClient) -> int | None:
    """How many tasks report ``live``, or ``None`` when the task service can't be reached (it's
    down mid-restart, which is expected — the summary then prints ``?`` rather than failing)."""
    try:
        tasks = client.list_tasks()
    except httpx.HTTPError:
        return None
    return sum(1 for task in tasks if task.get("container_status") == ContainerStatus.LIVE.value)


def _runner_live(client: TaskServiceClient) -> bool:
    """Whether a session-service runner holds its ``/runners/{id}/live`` connection — how we know
    the relaunched runner is actually back, not merely that tmux started something."""
    try:
        return bool(client.live_runners())
    except httpx.HTTPError:
        return False


def _stopped(
    name: str, *, run: CommandRunner, socket: str, ready: Callable[[str], bool], service_url: str
) -> bool:
    """Whether the killed session's process is really gone — the gate on relaunching it.

    The session disappearing is the general signal; for the task service we additionally require
    its URL to stop answering, which is direct proof the old uvicorn released its port. Relaunching
    over a socket the old process still holds would just crash the new one.
    """
    if _has_session(name, run=run, socket=socket):
        return False
    return name != SERVICE_SESSION or not ready(service_url)


def _is_back(
    name: str,
    *,
    run: CommandRunner,
    socket: str,
    ready: Callable[[str], bool],
    service_url: str,
    client: TaskServiceClient,
) -> bool:
    """Whether a relaunched session is serving again: the task service answers, the runner has
    reconnected its host-liveness stream, and anything else is up if its session still exists (a
    process that died on launch takes its session with it)."""
    if name == SERVICE_SESSION:
        return ready(service_url)
    if name == RUNNER_SESSION:
        return _runner_live(client)
    return _has_session(name, run=run, socket=socket)


def _settled(client: TaskServiceClient, before_live: int) -> bool:
    """Whether the task containers have finished re-registering with the restarted service."""
    return (live_task_count(client) or 0) >= before_live


def _wait(
    condition: Callable[[], bool],
    *,
    attempts: int,
    interval: float,
    sleep: Callable[[float], None],
) -> bool:
    """Poll ``condition`` until it holds, returning whether it did within ``attempts``."""
    for attempt in range(attempts):
        if condition():
            return True
        if attempt < attempts - 1:
            sleep(interval)
    return False


def _count(value: int | None) -> str:
    """A live count for the summary — ``?`` when the task service wasn't reachable."""
    return "?" if value is None else str(value)


def restart_sessions(
    targets: Sequence[str] = (),
    *,
    service_url: str,
    client: TaskServiceClient | None = None,
    switch_file: Path | None = None,
    run: CommandRunner = _run,
    ready: Callable[[str], bool] = service_ready,
    sleep: Callable[[float], None] = time.sleep,
    socket: str = TMUX_SOCKET,
    migrate: Callable[[], None] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Restart the control-plane sessions in place; returns a process exit code (0 ok, 1 failed).

    Service first — waiting until it answers again before the runner, which talks to it — then the
    runner, then the dashboard if it was asked for. Each session is stopped, **waited for**, and
    only then relaunched from :func:`background_sessions` /
    :func:`~panopticon.terminal.console.dashboard_command`. Every wait is bounded: a timeout prints
    to ``err`` and returns 1 rather than leaving the operator staring at a hung terminal.

    ``migrate``, when given, runs while the task service is down — between its old process exiting
    and the new one launching. Task sessions and their containers are never touched; the
    before/after summary (task sessions, tasks reporting ``live``) is there to show it.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    names = resolve_targets(targets)
    client = client or TaskServiceClient(httpx.Client(base_url=service_url))
    switch_file = switch_file or switch_file_path(socket)
    launch = dict(background_sessions())

    try:
        before_sessions = task_session_count(run=run, socket=socket)
    except FileNotFoundError:  # the first tmux call — report it, don't traceback
        print("tmux not found on PATH; restart needs it", file=err)
        return 1
    before_live = live_task_count(client)
    print(f"before: {before_sessions} task sessions, {_count(before_live)} live", file=out)

    for name in names:
        print(f"restarting {name}…", file=out)
        args = (
            dashboard_command(service_url, switch_file)
            if name == DASHBOARD_SESSION
            else [launch[name]]
        )
        if _has_session(name, run=run, socket=socket):
            run(_tmux("kill-session", "-t", name, socket=socket))
            stopped = _wait(
                partial(
                    _stopped,
                    name,
                    run=run,
                    socket=socket,
                    ready=ready,
                    service_url=service_url,
                ),
                attempts=STOP_ATTEMPTS,
                interval=STOP_INTERVAL,
                sleep=sleep,
            )
            if not stopped:
                print(f"{name}: still running after kill-session — not relaunching", file=err)
                return 1
        if name == SERVICE_SESSION and migrate is not None:
            # A restart usually follows a code pull, so apply any new migration (`upgrade head` is
            # idempotent) — with the old service down and the new one not yet bound, nothing is
            # reading the DB while it runs.
            migrate()
        run(_tmux("new-session", "-d", "-s", name, *args, socket=socket), check=True)
        back = _wait(
            partial(
                _is_back,
                name,
                run=run,
                socket=socket,
                ready=ready,
                service_url=service_url,
                client=client,
            ),
            attempts=READY_ATTEMPTS,
            interval=READY_INTERVAL,
            sleep=sleep,
        )
        if not back:
            print(f"{name}: did not come back up", file=err)
            return 1
        print(f"restarted {name}", file=out)

    # Container registrations are connection-scoped, so a restarted task service starts with none:
    # give the containers their reconnect window before reporting the after-count.
    if before_live:
        _wait(
            partial(_settled, client, before_live),
            attempts=SETTLE_ATTEMPTS,
            interval=SETTLE_INTERVAL,
            sleep=sleep,
        )
    after_sessions = task_session_count(run=run, socket=socket)
    after_live = live_task_count(client)
    print(f"after:  {after_sessions} task sessions, {_count(after_live)} live", file=out)
    if before_live and (after_live or 0) < before_live:
        print(
            f"warning: {before_live - (after_live or 0)} task(s) not back to live yet — "
            "they reconnect on their own; check the dashboard",
            file=err,
        )
    if DASHBOARD_SESSION in names:
        print("dashboard recreated detached — rejoin it with `panopticon console`", file=out)
    return 0
