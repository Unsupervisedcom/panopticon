"""CLI subcommand dispatch tests for `panopticon` / `python -m panopticon.terminal`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from panopticon.terminal.__main__ import main


def test_stop_kills_containers_and_server() -> None:
    ps_result = MagicMock()
    ps_result.stdout = "abc123\ndef456\n"
    with patch("subprocess.run", side_effect=[ps_result, MagicMock(), MagicMock()]) as mock_run:
        assert main(["stop"]) == 0
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0] == ["docker", "ps", "--all", "--quiet", "--filter", "label=panopticon.task"]
    assert calls[1] == ["docker", "rm", "--force", "abc123", "def456"]
    assert calls[2] == ["tmux", "-L", "panopticon", "kill-server"]


def test_stop_skips_docker_rm_when_no_containers() -> None:
    ps_result = MagicMock()
    ps_result.stdout = ""
    with patch("subprocess.run", side_effect=[ps_result, MagicMock()]) as mock_run:
        assert main(["stop"]) == 0
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert len(calls) == 2
    assert calls[0][0] == "docker"
    assert calls[1] == ["tmux", "-L", "panopticon", "kill-server"]
    assert not any(c[0] == "docker" and "rm" in c for c in calls)


def test_stop_tolerates_missing_docker_or_tmux() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert main(["stop"]) == 0


def test_build_dispatches_to_image_builder() -> None:
    with patch("panopticon.sessionservice.images.ImageBuilder") as mock_cls:
        assert main(["build"]) == 0
    mock_cls.return_value.build_base.assert_called_once_with(verbose=True)


def test_host_runs_migrate_then_sessions() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
    ):
        assert main(["host"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()


def test_no_arg_aliases_start() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main([]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()


def test_start_runs_migrate_sessions_then_console() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__._start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["start"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()
