"""CLI subcommand dispatch tests for `panopticon` / `python -m panopticon.terminal`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


def test_doctor_dispatches_to_the_checker_and_returns_its_code() -> None:
    with (
        patch("panopticon.terminal.doctor.run_checks", return_value=["sentinel"]) as mock_checks,
        patch("panopticon.terminal.doctor.report", return_value=3) as mock_report,
    ):
        assert main(["doctor"]) == 3
    mock_checks.assert_called_once_with()
    mock_report.assert_called_once_with(["sentinel"])


def test_host_runs_migrate_then_sessions() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__.start_sessions") as mock_sessions,
    ):
        assert main(["host"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()


def test_no_arg_aliases_start() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__.start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main([]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()


def test_start_runs_migrate_sessions_then_console() -> None:
    with (
        patch("panopticon.terminal.__main__._run_migrate") as mock_migrate,
        patch("panopticon.terminal.__main__.start_sessions") as mock_sessions,
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["start"]) == 0
    mock_migrate.assert_called_once_with()
    mock_sessions.assert_called_once_with()
    mock_console.assert_called_once()
    assert mock_console.call_args.kwargs["join"] is None  # no task → nothing to join


def test_start_with_a_task_arg_joins_it() -> None:
    # `panopticon start <task>` threads the task ref through to the console as `join=`.
    with (
        patch("panopticon.terminal.__main__._run_migrate"),
        patch("panopticon.terminal.__main__.start_sessions"),
        patch("panopticon.terminal.console.run_console_local") as mock_console,
    ):
        assert main(["start", "fix-login"]) == 0
    assert mock_console.call_args.kwargs["join"] == "fix-login"


def test_restart_dispatches_the_default_targets() -> None:
    # No target → the command decides (service + runner); `_run_migrate` is threaded in so a
    # restart after a code pull applies any new migration before the service comes back.
    with patch("panopticon.terminal.__main__.restart_sessions", return_value=0) as mock_restart:
        assert main(["restart"]) == 0
    assert mock_restart.call_args.args[0] == []
    assert mock_restart.call_args.kwargs["service_url"]
    assert mock_restart.call_args.kwargs["migrate"] is not None


def test_restart_passes_named_targets_through_and_returns_its_code() -> None:
    with patch("panopticon.terminal.__main__.restart_sessions", return_value=1) as mock_restart:
        assert main(["restart", "runner", "dashboard"]) == 1
    assert mock_restart.call_args.args[0] == ["runner", "dashboard"]


def test_restart_rejects_an_unknown_target() -> None:
    with pytest.raises(SystemExit):  # argparse rejects it before anything is bounced
        main(["restart", "containers"])


def test_the_service_client_reads_generously_but_connects_fast() -> None:
    from panopticon.terminal.__main__ import SERVICE_TIMEOUT

    assert SERVICE_TIMEOUT.read == 30.0
    assert SERVICE_TIMEOUT.connect == 5.0


def test_the_dashboard_client_is_built_with_the_service_timeout() -> None:
    from panopticon.terminal.__main__ import SERVICE_TIMEOUT

    with (
        patch("panopticon.terminal.__main__.httpx.Client") as mock_http,
        patch("panopticon.terminal.dashboard.run"),
    ):
        main(["dashboard"])
    assert mock_http.call_args.kwargs["timeout"] is SERVICE_TIMEOUT


def test_the_long_poll_still_overrides_the_service_timeout() -> None:
    import httpx

    from panopticon.client import TaskServiceClient
    from panopticon.terminal.__main__ import SERVICE_TIMEOUT

    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=[], headers={"X-Tasks-Version": "1"})

    http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler), timeout=SERVICE_TIMEOUT
    )
    client = TaskServiceClient(http)
    client.list_tasks_versioned(since=0, wait=20.0)
    client.list_tasks_versioned(since=0)
    assert seen[0]["read"] == 30.0  # wait + 10
    assert seen[0]["connect"] == 30.0  # the per-request Timeout(wait + 10) sets every phase
    assert seen[1] == SERVICE_TIMEOUT.as_dict()
