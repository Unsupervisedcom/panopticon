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

The same detach→attach mechanism also backs `v` (:func:`make_review_switch`), which opens **tarot**
on a task's work: it creates an on-demand ``panopticon-review-<id>`` session running tarot on the
task's local clone, records it, and detaches — so quitting tarot returns to the same dashboard,
exactly like detaching from a task session.
"""

from __future__ import annotations

import enum
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
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
#: prefix for the on-demand tarot **review** session a task opens with `v`. Kept distinct from the
#: task's own container session (`session_name` = ``panopticon-<id>``) so opening/killing a review
#: never touches the task's container.
REVIEW_SESSION_PREFIX = "panopticon-review-"


def review_session_name(task_id: str) -> str:
    """The tmux session name for a task's tarot **review** session (``v``), on the panopticon
    socket beside the task sessions. Distinct namespace from :func:`session_name`."""
    return f"{REVIEW_SESSION_PREFIX}{task_id}"


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


@dataclass(frozen=True)
class ReviewTarget:
    """What the dashboard hands the `v` hook to open tarot on a task's work: the task id (for the
    session name + the ``TAROT_PANOPTICON_TASK`` hint), its per-task ``clone`` on this host and the
    repo ``default_base`` (the local-clone review), its ``url`` (the fallback), and ``runner_host``
    (set → the clone lives on another host, so local-clone review is skipped)."""

    task_id: str
    clone: str | None
    url: str | None
    runner_host: str | None
    default_base: str


class ReviewResult(enum.Enum):
    """Outcome of a `v` press, mapped to a dashboard notify (or a silent hand-off)."""

    LAUNCHED = "launched"  # a fresh review session was created + attached (terminal handing off)
    REATTACHED = "reattached"  # a review session already existed — re-attached, no double-launch
    RELOADED = (
        "reloaded"  # existing session, but the clone advanced — tarot restarted, then attached
    )
    NO_TAROT = "no_tarot"  # tarot isn't installed on this host — nothing spawned
    NOTHING_TO_REVIEW = "nothing_to_review"  # no local clone and no url — nothing to open


#: The tmux **session** environment variable a review session records the clone HEAD it was launched
#: at, so a later `v` can tell whether the clone advanced (the agent committed) since. Set at
#: launch (`new-session -e`), updated on a reload (`set-environment`), read back on re-attach.
REVIEW_HEAD_SHA_VAR = "REVIEW_HEAD_SHA"


def _git_head_sha(clone: str) -> str | None:
    """The clone's current checked-out commit (``git rev-parse HEAD``), or ``None`` on any error.
    The clone is the agent's live ``/workspace``, so this advances as the agent commits — comparing
    it to the sha a review session launched at is the staleness signal."""
    result = subprocess.run(
        ["git", "-C", clone, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _tmux_show_env(session: str, key: str, *, socket: str) -> str | None:
    """Read a session environment variable via ``tmux show-environment`` (``None`` when unset or the
    session is gone). Output is ``KEY=value`` when set, ``-KEY`` when explicitly unset."""
    result = subprocess.run(
        ["tmux", "-L", socket, "show-environment", "-t", session, key],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line or line.startswith("-") or "=" not in line:
        return None
    return line.split("=", 1)[1] or None


def _git_configured_base(clone: str) -> str | None:
    """The clone's ``tarot.base`` git config, if the operator set one (else ``None``). When set we
    let tarot read its own config rather than forcing ``--base origin/<default_base>``."""
    result = subprocess.run(
        ["git", "-C", clone, "config", "--get", "tarot.base"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def _review_tmux_command(
    session: str,
    *,
    socket: str,
    cwd: str | None,
    env: dict[str, str],
    tarot_args: list[str],
) -> list[str]:
    """The argv that creates the detached tarot review session on the panopticon socket: a
    ``new-session`` started in ``cwd`` (the clone, when reviewing it in place) with ``env`` exported
    (``PANOPTICON_SERVICE_URL`` + the ``TAROT_PANOPTICON_TASK`` ask-the-author hint) running
    ``tarot`` with ``tarot_args``. The single place the review command is spelled — the unit tests
    pin it."""
    command = ["tmux", "-L", socket, "new-session", "-d", "-s", session]
    if cwd is not None:
        command += ["-c", cwd]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    command += ["tarot", *tarot_args]
    return command


def _set_session_env_command(session: str, *, socket: str, key: str, value: str) -> list[str]:
    """The argv that updates a review session's environment (``tmux set-environment``) — used to
    record the fresh clone HEAD on a reload so the *next* re-attach compares against it."""
    return ["tmux", "-L", socket, "set-environment", "-t", session, key, value]


def _respawn_review_command(
    session: str, *, socket: str, cwd: str, tarot_args: list[str]
) -> list[str]:
    """The argv that restarts tarot **in place** in an existing review session
    (``tmux respawn-window -k``) after the clone advanced. The respawned process inherits the
    session environment (the launch ``-e`` vars: ``PANOPTICON_SERVICE_URL``, ``TAROT_PANOPTICON_TASK``,
    the updated ``REVIEW_HEAD_SHA``) and the tmux **server** globals (``TAROT_ASK_CMD``), so the
    reload keeps them — the single place the reload command is spelled, pinned by the unit tests."""
    return [
        "tmux",
        "-L",
        socket,
        "respawn-window",
        "-k",
        "-t",
        session,
        "-c",
        cwd,
        "tarot",
        *tarot_args,
    ]


def _resolve_review(
    target: ReviewTarget,
    *,
    configured_base: Callable[[str], str | None],
    present: Callable[[str], bool],
) -> tuple[str | None, list[str], bool] | None:
    """Resolve a review target to ``(cwd, tarot_args, local)`` — the fallback ladder shared by the
    launch and the staleness re-attach so both spell the same tarot invocation. ``local`` is True
    for a **local-clone** review (present on this host, no ``runner_host``): only that case is
    staleness-checkable (it has an on-disk HEAD). Returns ``None`` when there's nothing to review.

    - local clone → ``(clone, ["--base", "origin/<default_base>"] | [], True)`` (``[]`` when the
      clone sets its own ``tarot.base``);
    - else a task ``url`` → ``(None, [url], False)``;
    - else ``None``."""
    if target.clone is not None and target.runner_host is None and present(target.clone):
        base = configured_base(target.clone)
        tarot_args = [] if base else ["--base", f"origin/{target.default_base}"]
        return target.clone, tarot_args, True
    if target.url:
        return None, [target.url], False
    return None


def make_review_switch(
    switch_file: Path,
    *,
    service_url: str,
    socket: str = TMUX_SOCKET,
    exists: Callable[[str], bool] | None = None,
    tarot_installed: Callable[[], bool] | None = None,
    configured_base: Callable[[str], str | None] = _git_configured_base,
    clone_present: Callable[[str], bool] | None = None,
    head_sha: Callable[[str], str | None] = _git_head_sha,
    stored_sha: Callable[[str], str | None] | None = None,
    run: Callable[[list[str]], object] | None = None,
    detach: Callable[[], None] = _tmux_detach,
) -> Callable[[ReviewTarget], ReviewResult]:
    """Build the dashboard's `v` hook: open tarot on a task's work, reusing the `t` switch-file
    detach/attach (:func:`switch_to`) so quitting tarot returns to the same dashboard.

    Read-only wrt the task — it never calls the task service; it only reads git state, creates or
    restarts a tmux session, and writes the switch-file. The fallback ladder
    (:func:`_resolve_review`): a **local** clone (present on this host, no ``runner_host``) is
    reviewed in place with ``--base origin/<default_base>`` (unless the clone sets ``tarot.base``);
    else the task's ``url`` (a PR) is opened; else there's nothing to review. With tarot not
    installed nothing is spawned.

    **Staleness-checked re-attach.** A review session is a *waiting* tarot showing the clone as of
    the sha it launched at (recorded in the session env, :data:`REVIEW_HEAD_SHA_VAR`). A second `v`:
    for a local-clone review, if the clone's current HEAD still equals the launch sha → a pure
    re-attach (instant, ``REATTACHED``); if it advanced (the agent committed) → the clone is already
    fresh on disk, so tarot is **restarted in place** (``respawn-window -k``, env preserved) and the
    session env updated, then attached (``RELOADED``). The url/remote case has no on-disk HEAD to
    diff, so it stays a pure re-attach. Every host interaction is injected for tests."""
    session_running = exists or (lambda s: session_exists(s, socket=socket))
    installed = tarot_installed or (lambda: shutil.which("tarot") is not None)
    present = clone_present or (lambda path: Path(path).is_dir())
    read_stored = stored_sha or (lambda s: _tmux_show_env(s, REVIEW_HEAD_SHA_VAR, socket=socket))
    launch = run or (lambda argv: subprocess.run(argv, check=False))

    def review(target: ReviewTarget) -> ReviewResult:
        if not installed():
            return ReviewResult.NO_TAROT
        session = review_session_name(target.task_id)
        resolved = _resolve_review(target, configured_base=configured_base, present=present)
        if session_running(session):  # a waiting tarot exists → re-attach (staleness-checked)
            result = ReviewResult.REATTACHED
            if resolved is not None and resolved[2]:  # local clone: diff launch sha vs HEAD now
                cwd, tarot_args, _ = resolved
                assert cwd is not None  # a local-clone review always carries its clone as cwd
                current = head_sha(cwd)
                launched_at = read_stored(session)
                if current and launched_at and current != launched_at:
                    # The clone advanced under the waiting tarot — restart it (env-preserving) so it
                    # re-reads the fresh HEAD, and record the new sha for the next re-attach.
                    launch(
                        _set_session_env_command(
                            session, socket=socket, key=REVIEW_HEAD_SHA_VAR, value=current
                        )
                    )
                    launch(
                        _respawn_review_command(
                            session, socket=socket, cwd=cwd, tarot_args=tarot_args
                        )
                    )
                    result = ReviewResult.RELOADED
            switch_to(session, switch_file=switch_file, detach=detach)
            return result
        if resolved is None:
            return ReviewResult.NOTHING_TO_REVIEW
        cwd, tarot_args, local = resolved
        env = {"PANOPTICON_SERVICE_URL": service_url, "TAROT_PANOPTICON_TASK": target.task_id}
        if local:  # record the sha tarot is loading, so a later `v` can detect the clone advancing
            assert cwd is not None
            sha = head_sha(cwd)
            if sha:
                env[REVIEW_HEAD_SHA_VAR] = sha
        command = _review_tmux_command(
            session, socket=socket, cwd=cwd, env=env, tarot_args=tarot_args
        )
        launch(command)
        switch_to(session, switch_file=switch_file, detach=detach)
        return ReviewResult.LAUNCHED

    return review


def list_review_sessions(
    *, socket: str = TMUX_SOCKET, run: Callable[[list[str]], object] | None = None
) -> set[str]:
    """The task ids that currently have a **warm** ``panopticon-review-<id>`` session on the
    panopticon socket — one ``tmux list-sessions`` call, so the dashboard can list once per refresh
    tick (not per row). Best-effort: no server / any tmux error → empty set (never crash a refresh)."""
    lister = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    try:
        result = lister(["tmux", "-L", socket, "list-sessions", "-F", "#{session_name}"])
    except Exception:
        return set()
    output = getattr(result, "stdout", "") or ""
    return {
        line[len(REVIEW_SESSION_PREFIX) :]
        for raw in output.splitlines()
        if (line := raw.strip()).startswith(REVIEW_SESSION_PREFIX)
    }


def reap_orphan_review_sessions(
    live_task_ids: set[str],
    *,
    socket: str = TMUX_SOCKET,
    sessions: set[str] | None = None,
    run: Callable[[list[str]], object] | None = None,
) -> list[str]:
    """Kill each warm ``panopticon-review-<id>`` session whose task is **gone** (its id not in
    ``live_task_ids`` — a deleted/reaped task): the review-session mirror of the spawner's
    terminal-container reaper, run on the console host where these sessions live. A still-listed
    task (even DROPPED/COMPLETE) keeps its review so it can still be inspected. Returns the ids
    reaped. Pass ``sessions`` (the already-listed warm set) to avoid a second ``list-sessions``."""
    warm = sessions if sessions is not None else list_review_sessions(socket=socket, run=run)
    killer = run or (lambda argv: subprocess.run(argv, check=False))
    reaped: list[str] = []
    for task_id in warm:
        if task_id not in live_task_ids:
            killer(["tmux", "-L", socket, "kill-session", "-t", review_session_name(task_id)])
            reaped.append(task_id)
    return reaped


def make_review_sessions_probe(*, socket: str = TMUX_SOCKET) -> Callable[[set[str]], set[str]]:
    """Build the dashboard's per-tick review-session probe: list the warm review sessions, reap any
    whose task is gone (:func:`reap_orphan_review_sessions`), and return the surviving warm task-id
    set (∩ the live ids) for the row **warm marker**. One ``tmux list-sessions`` per tick, shared by
    the marker and the reaper. Injected into the dashboard so it stays tmux-free and testable."""

    def probe(live_task_ids: set[str]) -> set[str]:
        warm = list_review_sessions(socket=socket)
        reap_orphan_review_sessions(live_task_ids, sessions=warm, socket=socket)
        return warm & live_task_ids

    return probe


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
