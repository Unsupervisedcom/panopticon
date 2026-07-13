"""Unit tests for panopticon.terminal.doctor — the host-prerequisite checks.

Every check is driven with fakes (a ``which`` probe, a command runner, an interpreter version)
so the tests never touch the real host or a subprocess.
"""

from __future__ import annotations

from collections.abc import Sequence

from panopticon.terminal import doctor
from panopticon.terminal.doctor import CheckResult


def _which_all_present(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _which_missing(*absent: str):
    def which(name: str) -> str | None:
        return None if name in absent else f"/usr/bin/{name}"

    return which


def _run_ok(command: Sequence[str]) -> int:
    return 0


def _run_fail(command: Sequence[str]) -> int:
    return 1


def _by_name(results: Sequence[CheckResult]) -> dict[str, CheckResult]:
    return {result.name: result for result in results}


def test_all_present_passes() -> None:
    results = doctor.run_checks(which=_which_all_present, run=_run_ok, version_info=(3, 11, 0))
    assert all(result.ok for result in results)
    assert doctor.report(results) == 0
    names = {result.name for result in results}
    assert names == {"python", "git", "docker", "tmux", "claude", "docker daemon"}


def test_missing_git_fails_with_hint() -> None:
    results = doctor.run_checks(which=_which_missing("git"), run=_run_ok, version_info=(3, 12, 1))
    git = _by_name(results)["git"]
    assert not git.ok
    assert git.hint
    assert doctor.report(results) == 1


def test_each_required_binary_is_checked() -> None:
    for binary in ("git", "docker", "tmux", "claude"):
        results = doctor.run_checks(
            which=_which_missing(binary), run=_run_ok, version_info=(3, 11, 0)
        )
        result = _by_name(results)[binary]
        assert not result.ok, binary
        assert doctor.report(results) == 1


def test_docker_present_but_daemon_down_fails() -> None:
    results = doctor.run_checks(which=_which_all_present, run=_run_fail, version_info=(3, 11, 0))
    by_name = _by_name(results)
    assert by_name["docker"].ok  # the binary is present
    assert not by_name["docker daemon"].ok  # but the daemon isn't reachable
    assert doctor.report(results) == 1


def test_docker_absent_skips_the_daemon_check() -> None:
    # No docker binary → report the one clear failure, not a second (redundant) daemon failure.
    results = doctor.run_checks(
        which=_which_missing("docker"), run=_run_fail, version_info=(3, 11, 0)
    )
    names = {result.name for result in results}
    assert "docker daemon" not in names
    assert not _by_name(results)["docker"].ok


def test_old_python_fails() -> None:
    results = doctor.run_checks(which=_which_all_present, run=_run_ok, version_info=(3, 10, 12))
    python = _by_name(results)["python"]
    assert not python.ok
    assert "3.11" in python.detail
    assert doctor.report(results) == 1


def test_render_lists_failing_name_and_hint() -> None:
    results = doctor.run_checks(which=_which_missing("tmux"), run=_run_ok, version_info=(3, 11, 0))
    text = doctor.render(results)
    assert "✗ tmux" in text
    assert _by_name(results)["tmux"].hint in text
    assert "tmux" in text.splitlines()[-1]  # named in the summary


def test_check_binary_reports_the_resolved_path() -> None:
    result = doctor.check_binary("git", hint="x", which=lambda _name: "/opt/bin/git")
    assert result.ok
    assert "/opt/bin/git" in result.detail
