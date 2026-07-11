"""ShellRunner: unit tests pin the emitted tmux commands + the assembled shell command. No tmux —
the command runner is a fake that records calls. LLM-free (a shell task runs no agent)."""

from __future__ import annotations

from collections.abc import Sequence

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.shell_runner import ShellRunner


class _Recorder:
    """An injectable CommandRunner that records calls and replays a queued stdout per call."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def __call__(self, args: Sequence[str], *, check: bool = True, interactive: bool = False, verbose: bool = False) -> str:
        self.calls.append(list(args))
        return self._stdout


def test_shell_runner_is_a_runner() -> None:
    assert issubclass(ShellRunner, Runner)


def test_spawn_kills_stale_session_then_starts_the_script() -> None:
    rec = _Recorder()
    runner = ShellRunner("http://svc:8000", runner_id="r1", run=rec)

    session = runner.spawn("t1", script="claude setup-token")

    assert session == "panopticon-t1"
    kill, new_session = rec.calls
    # a stale session of the same name is cleared first (idempotent restart)
    assert kill == ["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]
    assert new_session[:6] == ["tmux", "-L", "panopticon", "new-session", "-d", "-s"]
    assert new_session[6] == "panopticon-t1"
    assert new_session[7:9] == ["sh", "-c"]  # the pane runs the assembled script under sh -c


def test_spawn_exports_service_env_and_runs_the_script() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", runner_id="r1", run=rec).spawn("t1", script="claude setup-token")
    command = rec.calls[-1][-1]  # the sh -c argument
    assert "export PANOPTICON_SERVICE_URL=http://svc:8000" in command
    assert "export PANOPTICON_TASK_ID=t1" in command
    assert "export PANOPTICON_RUNNER_ID=r1" in command
    assert command.rstrip().endswith("claude setup-token")  # the workflow script runs last


def test_spawn_sources_the_env_file_when_given() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi", env_file="/sec/r1.env")
    command = rec.calls[-1][-1]
    assert "set -a; . /sec/r1.env; set +a" in command  # secrets sourced before the script


def test_spawn_omits_env_sourcing_without_a_file() -> None:
    rec = _Recorder()
    ShellRunner("http://svc:8000", run=rec).spawn("t1", script="echo hi")
    assert ". " not in rec.calls[-1][-1]  # no source line


def test_spawn_reports_starting_then_awaiting() -> None:
    phases: list[LifecyclePhase] = []
    ShellRunner("http://svc:8000", run=_Recorder()).spawn("t1", script="echo hi", progress=phases.append)
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
