"""HostRunner: unit tests pin the emitted tmux commands and the assembled pane command. No tmux —
the command runner is a fake that records calls. LLM-free (the agent runs in the pane, not here)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.host_runner import (
    AGENT_MODULE,
    LIVENESS_MODULE,
    HostRunner,
    task_home,
)
from panopticon.sessionservice.runner import Runner


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


def _pane_command(calls: list[list[str]]) -> str:
    """The `sh -c` payload of the recorded `new-session` call."""
    new_session = next(c for c in calls if "new-session" in c)
    return new_session[-1]


def test_host_runner_is_a_runner() -> None:
    assert issubclass(HostRunner, Runner)


def test_spawn_kills_stale_session_then_starts_the_agent_in_the_workspace(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", runner_id="r1", run=rec, homes_root=tmp_path)

    session = runner.spawn("t1", workspace="/tasks/t1")

    assert session == "panopticon-t1"
    kill, new_session = rec.calls
    # a stale session of the same name is cleared first, so a respawn is a restart not a second one
    assert kill == ["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]
    assert new_session[:6] == ["tmux", "-L", "panopticon", "new-session", "-d", "-s"]
    assert new_session[6] == "panopticon-t1"
    # the pane starts in the per-task clone — the agent's cwd, as /workspace is in a container
    assert new_session[7:9] == ["-c", "/tasks/t1"]
    assert new_session[9:11] == ["sh", "-c"]


def test_spawn_runs_liveness_in_the_background_and_the_agent_in_the_foreground(
    tmp_path: Path,
) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, python="/venv/bin/python", homes_root=tmp_path)

    runner.spawn("t1", workspace="/tasks/t1")
    command = _pane_command(rec.calls)

    # the container's PID 1 + `docker exec` pair, as one pane: liveness backgrounded, agent exec'd
    assert f"/venv/bin/python -m {LIVENESS_MODULE} >/dev/null 2>&1 &" in command
    assert command.rstrip().endswith(f"exec /venv/bin/python -m {AGENT_MODULE}")
    # the backgrounded half is reaped when the agent returns, not left holding a registration
    assert "trap 'kill $_panopticon_live_pid 2>/dev/null' EXIT" in command


def test_spawn_never_launches_the_container_agent_module(tmp_path: Path) -> None:
    """The container launcher ends by signalling PID 1 — on a host that is init, so the pane must
    run the host wrapper instead. Pinned because the two module names differ by one segment."""
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    runner.spawn("t1", workspace="/tasks/t1")
    command = _pane_command(rec.calls)

    assert "panopticon.container.agent" not in command
    assert AGENT_MODULE == "panopticon.container.host"


def test_spawn_points_home_at_the_task_home_and_creates_it(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    runner.spawn("t1", workspace="/tasks/t1")

    home = tmp_path / "t1"
    assert home.is_dir()  # created before the session, so claude has somewhere to write
    assert f"export HOME={home}" in _pane_command(rec.calls)


def test_task_home_is_outside_the_workspace(tmp_path: Path) -> None:
    """The workspace is a git checkout: a home written into it would be untracked files in every
    `git status` the agent runs."""
    assert not task_home("t1", homes_root=tmp_path).startswith("/tasks/t1")


def test_spawn_sources_the_env_file_before_exporting_the_protocol_variables(
    tmp_path: Path,
) -> None:
    """`set -a` exports everything the operator's file assigns, so sourcing it after panopticon's
    own variables would let a stray PANOPTICON_* in it displace the protocol's."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "repo.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, secrets_dir=secrets, homes_root=tmp_path)

    runner.spawn("t1", env_file="repo.env", workspace="/tasks/t1")
    command = _pane_command(rec.calls)

    source_at = command.index(str(secrets / "repo.env"))
    task_id_at = command.index("export PANOPTICON_TASK_ID=")
    assert source_at < task_id_at


def test_spawn_passes_the_prompt_turn_and_model_through_to_the_launcher(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    runner.spawn(
        "t1",
        workspace="/tasks/t1",
        initial_prompt="do the thing",
        turn="agent",
        starting_model="opus",
    )
    command = _pane_command(rec.calls)

    assert "export PANOPTICON_INITIAL_PROMPT='do the thing'" in command
    assert "export PANOPTICON_TASK_TURN=agent" in command
    assert "export PANOPTICON_STARTING_MODEL=opus" in command


def test_spawn_omits_the_optional_variables_when_unset(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    runner.spawn("t1", workspace="/tasks/t1")
    command = _pane_command(rec.calls)

    assert "PANOPTICON_INITIAL_PROMPT" not in command
    assert "PANOPTICON_TASK_TURN" not in command
    assert "PANOPTICON_STARTING_MODEL" not in command


def test_spawn_reports_starting_then_awaiting_and_never_building(tmp_path: Path) -> None:
    """A host task has no image, so it skips BUILDING — the phase sequence the dashboard shows."""
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)
    phases: list[LifecyclePhase] = []

    runner.spawn("t1", workspace="/tasks/t1", progress=phases.append)

    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]


def test_has_session_matches_the_task_and_only_the_task(tmp_path: Path) -> None:
    rec = _Recorder(stdout="panopticon-t1\npanopticon-t22\nservice\n")
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    assert runner.has_session("t1")
    assert runner.has_session("t22")
    assert not runner.has_session("t2")  # a prefix of another session is not a match


def test_is_running_is_the_session_for_a_host_task(tmp_path: Path) -> None:
    """The pane holds both halves, so the session lives exactly as long as the agent."""
    rec = _Recorder(stdout="panopticon-t1\n")
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    assert runner.is_running("t1")
    assert not runner.is_running("gone")


def test_stop_kills_the_session_and_tolerates_a_missing_one(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)

    runner.stop("panopticon-t1")

    assert rec.calls == [["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-t1"]]


def test_delete_home_removes_the_task_home_and_is_idempotent(tmp_path: Path) -> None:
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)
    home = tmp_path / "t1"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "advance.md").write_text("x")

    runner.delete_home("t1")
    runner.delete_home("t1")  # nothing left to remove

    assert not home.exists()


def test_delete_workspace_contents_empties_without_docker(tmp_path: Path) -> None:
    """The host counterpart of the Docker runner's throwaway-root-container trick — it must work on
    a machine with no Docker at all."""
    rec = _Recorder()
    runner = HostRunner("http://svc:8000", run=rec, homes_root=tmp_path)
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("x")
    (workspace / "README").write_text("y")

    runner.delete_workspace_contents(str(workspace))

    assert workspace.is_dir() and list(workspace.iterdir()) == []
    assert rec.calls == []  # no docker, no subprocess at all
