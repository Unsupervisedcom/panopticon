"""ShellRunner: unit tests pin the emitted tmux commands + the assembled shell command. No tmux —
the command runner is a fake that records calls. LLM-free (a shell task runs no agent)."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.shell_runner import ShellRunner


class _Recorder:
    """An injectable CommandRunner that records calls and replays a queued stdout per call."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        self.calls.append(list(args))
        return self._stdout


def test_shell_runner_is_a_runner() -> None:
    assert issubclass(ShellRunner, Runner)


def test_spawn_kills_stale_session_then_starts_the_script_in_the_task_dir() -> None:
    rec = _Recorder()
    runner = ShellRunner("http://svc:8000", runner_id="r1", run=rec)

    session = runner.spawn("t1", script="claude setup-token", workdir="/tasks/t1")

    assert session == "panopticon-t1"
    kill, new_session = rec.calls
    # a stale session of the same name is cleared first (idempotent restart)
    assert kill == ["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]
    assert new_session[:6] == ["tmux", "-L", "panopticon", "new-session", "-d", "-s"]
    assert new_session[6] == "panopticon-t1"
    assert new_session[7:9] == ["-c", "/tasks/t1"]  # the pane starts in the task's own directory
    assert new_session[9:11] == ["sh", "-c"]  # the pane runs the assembled script under sh -c


def test_spawn_falls_back_to_the_operator_home_without_a_workdir() -> None:
    import os

    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")  # no workdir → home
    new_session = rec.calls[-1]
    assert new_session[new_session.index("-c") + 1] == os.path.expanduser("~")


def test_spawn_exports_service_env_and_runs_the_script() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", runner_id="r1", run=rec).spawn("t1", script="claude setup-token")
    command = rec.calls[-1][-1]  # the sh -c argument
    assert "export PANOPTICON_SERVICE_URL=http://svc:8000" in command
    assert "export PANOPTICON_TASK_ID=t1" in command
    assert "export PANOPTICON_RUNNER_ID=r1" in command
    assert command.rstrip().endswith("claude setup-token")  # the workflow script runs last


def test_spawn_loads_the_panopticon_shell_lib_before_the_script() -> None:
    # The shell lib (task_lib.sh) is injected so the workflow script can drive its task over REST
    # (panopticon_advance/_drop/…) instead of hand-rolling curl.
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="panopticon_advance")
    command = rec.calls[-1][-1]
    assert "panopticon_advance()" in command  # the function is defined...
    assert "_panopticon_api" in command  # ...along with the lib internals
    assert command.index("panopticon_advance()") < command.rindex(
        "panopticon_advance"
    )  # def before use


def test_spawn_holds_a_liveness_registration_open_in_the_background() -> None:
    # A shell task runs no agent, so the runner holds its /live stream open so the dashboard shows
    # it live (not `awaiting`) while the script runs; a trap drops it when the script exits.
    rec = _Recorder()
    ShellRunner("http://svc:8000", runner_id="r1", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert (
        "/tasks/t1/live?container_id=panopticon-t1&runner_id=r1" in command
    )  # holds liveness open
    assert "--no-buffer" in command and command.count(" &\n") >= 1  # backgrounded, streaming GET
    assert "trap 'kill $_panopticon_live_pid 2>/dev/null' EXIT" in command  # dropped when it exits
    # the registration is established before the workflow script runs
    assert command.index("/live?") < command.index("echo hi")


def test_spawn_resolves_and_sources_the_env_file_against_the_secrets_dir() -> None:
    # env_file is a name relative to this runner's secrets dir (ADR 0007), resolved host-locally.
    rec = _Recorder()
    ShellRunner("http://svc:8000", secrets_dir="/host/secrets", run=rec).spawn(
        "t1", script="echo hi", env_file="r1.env"
    )
    command = rec.calls[-1][-1]
    assert (
        "export PANOPTICON_ENV_FILE=/host/secrets/r1.env" in command
    )  # path exposed to the script
    # resolved + sourced (guarded on existence — a not-yet-created secrets file is fine)
    assert "[ -f /host/secrets/r1.env ]" in command
    assert "set -a; . /host/secrets/r1.env; set +a" in command


def test_spawn_rejects_an_env_file_name_escaping_the_secrets_dir() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match="escapes the secrets dir"):
        ShellRunner("http://svc:8000", secrets_dir="/host/secrets", run=rec).spawn(
            "t1", script="echo hi", env_file="../evil.env"
        )


def test_spawn_omits_env_sourcing_without_a_file() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    command = rec.calls[-1][-1]
    assert "set -a" not in command and "PANOPTICON_ENV_FILE" not in command  # no source line


def test_spawn_reports_starting_then_awaiting() -> None:
    phases: list[LifecyclePhase] = []
    ShellRunner("http://svc:8000", run=_Recorder()).spawn(
        "t1", script="echo hi", progress=phases.append
    )
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]  # no PREPARING/BUILDING


def test_has_session_and_is_running_match_the_session_list() -> None:
    present = _Recorder(stdout="panopticon-t1\npanopticon-t2\n")
    runner = ShellRunner("http://svc:8000", run=present)
    assert runner.has_session("t1") is True
    assert runner.is_running("t1") is True  # for a shell task, the session IS its liveness

    absent = _Recorder(stdout="panopticon-other\n")
    runner_absent = ShellRunner("http://svc:8000", run=absent)
    assert runner_absent.has_session("t1") is False
    assert runner_absent.is_running("t1") is False


def test_stop_kills_the_session() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).stop("panopticon-t1")
    assert rec.calls == [["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]]


# -- integration: a real host tmux session (no container) ---------------------------


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_spawn_runs_the_script_in_a_real_tmux_session(tmp_path: Path) -> None:
    # Proves a shell task runs in a live host tmux session (no container): the script executes in
    # the pane (drops a marker), the session is attachable while it runs, and stop() tears it down.
    socket = "panopticon-shelltest"
    runner = ShellRunner("http://unused", tmux_socket=socket)
    marker = tmp_path / "ran"
    session = "panopticon-itest1"
    try:
        # touch a marker (the script really ran in the pane), then sleep so the session stays up
        assert (
            runner.spawn("itest1", script=f"touch {marker}; sleep 30", workdir=str(tmp_path))
            == session
        )
        for _ in range(50):  # new-session returns once the pane is up; poll defensively
            if runner.has_session("itest1") and marker.exists():
                break
            time.sleep(0.1)
        assert runner.has_session("itest1")  # a live tmux session the operator could `t`-attach to
        assert runner.is_running("itest1")  # the session is the shell task's liveness
        assert marker.exists()  # the script executed inside the pane
    finally:
        runner.stop(session)
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    assert not runner.has_session("itest1")  # stop() killed it
