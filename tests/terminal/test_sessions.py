"""The background control-plane sessions: starting them, and `panopticon restart`'s in-place bounce.

The command runner, the service-readiness probe, the sleep, and the task-service client are all
injected, so every emitted `tmux` argv (and its ordering) is pinned without tmux, Docker, or HTTP.
The point of the restart is that it touches **only** the `service`/`runner`/`dashboard` sessions —
never `docker`, never a `panopticon-<task-id>` task session — so that's asserted directly.
"""

from __future__ import annotations

import io
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from panopticon.terminal import sessions
from panopticon.terminal.sessions import (
    DEFAULT_TARGETS,
    background_command,
    resolve_targets,
    restart_sessions,
    start_sessions,
)

SERVICE_URL = "http://localhost:8000"
#: Two task sessions + the three control-plane ones, as `tmux list-sessions -F '#{session_name}'`
#: would report them.
SESSIONS = "service\nrunner\ndashboard\npanopticon-aaa\npanopticon-bbb\n"


class _Tmux:
    """A fake command runner: records every argv, answers `has-session`/`list-sessions`.

    ``live`` is the set of sessions that exist; ``kill-session`` removes one and ``new-session``
    adds it, so a restart's has-session probes see the real sequence.
    """

    def __init__(self, live: set[str] | None = None, *, listing: str = SESSIONS) -> None:
        self.live = {"service", "runner", "dashboard"} if live is None else set(live)
        self.listing = listing
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        verb = argv[3]
        stdout = ""
        code = 0
        if verb == "has-session":
            code = 0 if argv[-1] in self.live else 1
        elif verb == "list-sessions":
            stdout = self.listing
        elif verb == "kill-session":
            self.live.discard(argv[-1])
        elif verb == "new-session":
            self.live.add(argv[argv.index("-s") + 1])
        return subprocess.CompletedProcess(list(argv), code, stdout=stdout, stderr="")

    def verbs(self) -> list[tuple[str, str]]:
        """The mutating calls as ``(verb, session)`` pairs — the ordering the tests pin."""
        return [
            (c[3], c[c.index("-s") + 1] if "-s" in c else c[-1])
            for c in self.calls
            if c[3] in ("kill-session", "new-session")
        ]


class _Client:
    """A fake task-service client: canned tasks + whether a runner is connected."""

    def __init__(self, live_tasks: int = 2, *, runner: bool = True) -> None:
        self.live_tasks = live_tasks
        self.runner = runner

    def list_tasks(self) -> list[dict[str, Any]]:
        return [{"id": str(i), "container_status": "live"} for i in range(self.live_tasks)] + [
            {"id": "queued", "container_status": "queued"}
        ]

    def live_runners(self) -> list[dict[str, Any]]:
        return [{"id": "local", "host": "h"}] if self.runner else []


def _restart(
    targets: tuple[str, ...] = (),
    *,
    tmux: _Tmux | None = None,
    client: _Client | None = None,
    ready: Any = None,
    out: io.StringIO | None = None,
    err: io.StringIO | None = None,
) -> tuple[int, _Tmux, io.StringIO, io.StringIO]:
    """Run a restart with everything injected — no tmux, no HTTP, no sleeping."""
    tmux = tmux or _Tmux()
    out, err = out or io.StringIO(), err or io.StringIO()
    # The service answers before the kill and after the relaunch; in between it's down. The fake
    # tmux's session set is the source of truth, so key readiness off it.
    probe = ready or (lambda url: "service" in tmux.live)
    code = restart_sessions(
        targets,
        service_url=SERVICE_URL,
        client=client or _Client(),  # type: ignore[arg-type]
        switch_file=Path("/tmp/panopticon-console-panopticon/switch"),
        run=tmux,
        ready=probe,
        sleep=lambda _: None,
        out=out,
        err=err,
    )
    return code, tmux, out, err


# -- the launch table (shared by start and restart) -------------------------------------------


def test_background_command_quotes_a_python_path_with_spaces() -> None:
    # A pipx install on macOS lives under `~/Library/Application Support/...`; the space in the
    # interpreter path would word-split when the launch command runs through the shell (tmux runs
    # `new-session`'s command via `/bin/sh -c`), so the path must be shlex-quoted. Regression for
    # `zsh: no such file or directory: /Users/.../Library/Application`.
    fake_executable = "/Users/x/Library/Application Support/pipx/venvs/panopticon/bin/python"
    with patch("panopticon.terminal.sessions.sys.executable", fake_executable):
        cmd = background_command("panopticon.taskservice", "/tmp/panopticon-service.log")

    assert shlex.quote(fake_executable) in cmd  # the quoted path is present…
    # …and the bare, unquoted path is not — i.e. the command is safe through `/bin/sh -c`.
    assert f"{fake_executable} -m" not in cmd
    # It parses as a single argv token, not two (the whole point of quoting).
    assert shlex.split(cmd)[0] == fake_executable


def test_start_sessions_starts_only_what_is_not_running() -> None:
    tmux = _Tmux(live={"service"})  # the service is up; only the runner should be launched
    start_sessions(run=tmux)
    assert tmux.verbs() == [("new-session", "runner")]
    launched = next(c for c in tmux.calls if c[3] == "new-session")[-1]
    assert "-m panopticon.sessionservice.host" in launched
    assert "tee /tmp/panopticon-runner.log" in launched


def test_start_sessions_launches_both_on_a_cold_socket() -> None:
    tmux = _Tmux(live=set())
    start_sessions(run=tmux)
    assert tmux.verbs() == [("new-session", "service"), ("new-session", "runner")]


def test_restart_launches_the_same_commands_start_does() -> None:
    _, tmux, _, _ = _restart()
    started = {c[c.index("-s") + 1]: c[-1] for c in tmux.calls if c[3] == "new-session"}
    cold = _Tmux(live=set())
    start_sessions(run=cold)
    assert started == {c[c.index("-s") + 1]: c[-1] for c in cold.calls if c[3] == "new-session"}


# -- target selection ---------------------------------------------------------------------------


def test_resolve_targets_defaults_to_service_and_runner() -> None:
    assert resolve_targets([]) == list(DEFAULT_TARGETS) == ["service", "runner"]


def test_resolve_targets_all_includes_the_dashboard_last() -> None:
    assert resolve_targets(["all"]) == ["service", "runner", "dashboard"]


def test_resolve_targets_keeps_restart_order_and_deduplicates() -> None:
    assert resolve_targets(["runner", "service", "runner"]) == ["service", "runner"]


def test_restart_bounces_only_the_named_target() -> None:
    _, tmux, _, _ = _restart(("runner",))
    assert tmux.verbs() == [("kill-session", "runner"), ("new-session", "runner")]


def test_restart_all_includes_the_dashboard_with_its_console_argv() -> None:
    _, tmux, out, _ = _restart(("all",))
    assert [v for v in tmux.verbs() if v[0] == "new-session"] == [
        ("new-session", "service"),
        ("new-session", "runner"),
        ("new-session", "dashboard"),
    ]
    # It's relaunched with the console supervisor's own argv, switch-file and all.
    dashboard = next(
        c for c in tmux.calls if c[3] == "new-session" and c[c.index("-s") + 1] == "dashboard"
    )
    assert dashboard[-7:] == [
        "-m",
        "panopticon.terminal",
        "--service-url",
        SERVICE_URL,
        "dashboard",
        "--switch-file",
        "/tmp/panopticon-console-panopticon/switch",
    ]
    # Restarting it detaches an attached supervisor, so say how to get back.
    assert "panopticon console" in out.getvalue()


# -- ordering + the safety properties -----------------------------------------------------------


def test_restart_stops_then_starts_the_service_before_the_runner() -> None:
    _, tmux, _, _ = _restart()
    assert tmux.verbs() == [
        ("kill-session", "service"),
        ("new-session", "service"),
        ("kill-session", "runner"),
        ("new-session", "runner"),
    ]


def test_restart_waits_for_the_old_service_to_release_its_port_before_relaunching() -> None:
    # The session is gone but the URL still answers (uvicorn hasn't let go of :8000 yet): the
    # relaunch must wait, else the new process crashes on bind.
    answers = iter([True, True, False] + [True] * 50)
    seen: list[bool] = []

    def ready(_url: str) -> bool:
        value = next(answers)
        seen.append(value)
        return value

    code, tmux, _, _ = _restart(("service",), ready=ready)
    assert code == 0
    kills = [i for i, c in enumerate(tmux.calls) if c[3] == "kill-session"]
    starts = [i for i, c in enumerate(tmux.calls) if c[3] == "new-session"]
    # Three readiness probes happened between the kill and the relaunch: the two that still
    # answered (so we kept waiting) and the one that didn't (so we went ahead).
    probes_before_relaunch = seen[:3]
    assert probes_before_relaunch == [True, True, False]
    assert kills[0] < starts[0]


def test_restart_never_touches_docker_or_a_task_session() -> None:
    _, tmux, _, _ = _restart(("all",))
    for call in tmux.calls:
        assert call[0] == "tmux"  # nothing but tmux — never `docker`
        if call[3] in ("kill-session", "new-session"):
            assert not any(arg.startswith("panopticon-") for arg in call)
    # The only place task sessions appear is the read-only count.
    assert [c[3] for c in tmux.calls if c[3] == "list-sessions"]


def test_restart_starts_a_session_that_is_not_running_without_killing_it() -> None:
    tmux = _Tmux(live={"runner"})  # the service died earlier — just bring it back
    _restart(("service",), tmux=tmux)
    assert tmux.verbs() == [("new-session", "service")]


def test_restart_migrates_while_the_service_is_down() -> None:
    # A restart usually follows a code pull, so migrations run — but in the window where the old
    # service has exited and the new one hasn't launched, so nothing is reading the DB.
    calls: list[bool] = []
    tmux = _Tmux()
    restart_sessions(
        service_url=SERVICE_URL,
        client=_Client(),  # type: ignore[arg-type]
        switch_file=Path("/switch"),
        run=tmux,
        ready=lambda _url: "service" in tmux.live,
        sleep=lambda _: None,
        migrate=lambda: calls.append("service" in tmux.live),
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert calls == [False]  # ran exactly once, with the service session down


def test_restart_skips_migrations_when_the_service_is_not_a_target() -> None:
    calls: list[str] = []
    tmux = _Tmux()
    restart_sessions(
        ("runner",),
        service_url=SERVICE_URL,
        client=_Client(),  # type: ignore[arg-type]
        switch_file=Path("/switch"),
        run=tmux,
        ready=lambda _url: True,
        sleep=lambda _: None,
        migrate=lambda: calls.append("migrate"),
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert calls == []


# -- bounded waits fail loudly -------------------------------------------------------------------


def test_restart_gives_up_when_the_old_service_never_releases_the_port() -> None:
    code, tmux, _, err = _restart(("service",), ready=lambda _url: True)  # answers forever
    assert code == 1
    assert "still running" in err.getvalue()
    assert not [c for c in tmux.calls if c[3] == "new-session"]  # never relaunched on top of it


def test_restart_gives_up_when_the_new_service_never_answers() -> None:
    code, tmux, _, err = _restart(("service",), ready=lambda _url: False)  # never comes up
    assert code == 1
    assert "did not come back up" in err.getvalue()
    assert ("new-session", "service") in tmux.verbs()  # it was launched, it just never answered


def test_restart_gives_up_when_the_runner_never_reconnects() -> None:
    code, _, _, err = _restart(("runner",), client=_Client(runner=False))
    assert code == 1
    assert "runner: did not come back up" in err.getvalue()


def test_restart_reports_a_missing_tmux_instead_of_raising() -> None:
    def missing(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    err = io.StringIO()
    code = restart_sessions(
        service_url=SERVICE_URL,
        client=_Client(),  # type: ignore[arg-type]
        switch_file=Path("/switch"),
        run=missing,
        ready=lambda _url: True,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=err,
    )
    assert code == 1
    assert "tmux not found" in err.getvalue()


# -- the before/after summary ---------------------------------------------------------------------


def test_restart_prints_the_before_and_after_summary() -> None:
    _, _, out, err = _restart()
    lines = out.getvalue().splitlines()
    assert lines[0] == "before: 2 task sessions, 2 live"
    assert lines[-1] == "after:  2 task sessions, 2 live"
    assert err.getvalue() == ""  # nothing was interrupted


def test_restart_warns_when_a_task_has_not_come_back_to_live() -> None:
    class _Dropping(_Client):
        """Reports one fewer live task after the restart — a container that hasn't re-registered."""

        def __init__(self) -> None:
            super().__init__(live_tasks=3)
            self.reads = 0

        def list_tasks(self) -> list[dict[str, Any]]:
            self.reads += 1
            if self.reads > 1:
                self.live_tasks = 2
            return super().list_tasks()

    _, _, out, err = _restart(client=_Dropping())
    assert "before: 2 task sessions, 3 live" in out.getvalue()
    assert "after:  2 task sessions, 2 live" in out.getvalue()
    assert "1 task(s) not back to live yet" in err.getvalue()


def test_restart_summary_tolerates_an_unreachable_task_service() -> None:
    import httpx

    class _Down(_Client):
        def list_tasks(self) -> list[dict[str, Any]]:
            raise httpx.ConnectError("no service")

    code, _, out, _ = _restart(("runner",), client=_Down())
    assert code == 0
    assert "before: 2 task sessions, ? live" in out.getvalue()


# -- integration: a real tmux server ---------------------------------------------------------


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_restart_replaces_its_session_and_leaves_task_sessions_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole point of `panopticon restart`, against a real tmux server: the control-plane
    # session is killed and relaunched (a new process runs in it), while the sibling task session
    # keeps running untouched. The launch table is stubbed to `sleep` so no real daemon is started.
    socket = "panopticon-restarttest"
    monkeypatch.setattr(
        sessions, "background_sessions", lambda: [("service", "sleep 60"), ("runner", "sleep 60")]
    )

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True)

    def pid(session: str) -> str:
        return tmux("list-panes", "-t", session, "-F", "#{pane_pid}").stdout.strip()

    try:
        tmux("new-session", "-d", "-s", "runner", "sleep 60")
        tmux("new-session", "-d", "-s", "panopticon-itest", "sleep 60")
        before = pid("runner")
        task_before = pid("panopticon-itest")

        code = restart_sessions(
            ("runner",),
            service_url=SERVICE_URL,
            client=_Client(),  # type: ignore[arg-type]
            switch_file=tmp_path / "switch",
            socket=socket,
            ready=lambda _url: True,
            out=io.StringIO(),
            err=io.StringIO(),
        )

        assert code == 0
        assert tmux("has-session", "-t", "runner").returncode == 0
        assert pid("runner") != before  # a new process is running in it
        assert tmux("has-session", "-t", "panopticon-itest").returncode == 0
        assert pid("panopticon-itest") == task_before  # the task session never noticed
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
