"""Tests for ``panopticon doctor``: drive each check with a fake CommandRunner and in-memory
filesystem; assert OK/WARN/FAIL classification and the exact probe commands emitted. No LLM,
no Docker, no tmux required."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from panopticon.terminal.doctor import (
    CommandRunner,
    check_base_image,
    check_docker,
    check_git,
    check_repo_env_file,
    check_task_service,
    check_tmux,
    run_doctor,
)


class _FakeRunner:
    """Injectable CommandRunner: records calls; succeeds by default. Configure failures per command."""

    def __init__(
        self,
        *,
        outputs: dict[str, str] | None = None,
        failures: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._outputs = outputs or {}
        self._failures = failures or set()

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        key = args[0] if args else ""
        self.calls.append(list(args))
        if key in self._failures:
            raise FileNotFoundError(f"{key}: not found")
        return self._outputs.get(key, "")


#: A runner where every host tool (docker/git/tmux) is present — the all-green baseline.
def _healthy_runner() -> _FakeRunner:
    return _FakeRunner(outputs={
        "docker": "Server: Docker Desktop",
        "git": "git version 2.43.0",
        "tmux": "tmux 3.3a",
    })


# ---------------------------------------------------------------------------
# check_docker
# ---------------------------------------------------------------------------

def test_check_docker_ok() -> None:
    runner = _FakeRunner(outputs={"docker": "Server: Docker Desktop\nversion: 24.0"})
    result = check_docker(run=runner, platform="linux")
    assert result.status == "OK"
    assert runner.calls[0] == ["docker", "info"]


def test_check_docker_fail_daemon_not_running() -> None:
    runner = _FakeRunner(failures={"docker"})
    result = check_docker(run=runner, platform="linux")
    assert result.status == "FAIL"
    assert "not reachable" in result.message
    assert result.remediation is not None


def test_check_docker_fail_raises_called_process_error() -> None:
    class _FailRunner:
        def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
            raise subprocess.CalledProcessError(1, list(args))

    result = check_docker(run=_FailRunner(), platform="linux")
    assert result.status == "FAIL"


def test_check_docker_macos_warn_when_desktop_absent() -> None:
    runner = _FakeRunner(outputs={"docker": "Server: Docker Engine\nOs: linux"})
    result = check_docker(run=runner, platform="darwin")
    assert result.status == "WARN"
    assert "Docker Desktop" in result.message
    assert result.remediation is not None


def test_check_docker_macos_ok_when_desktop_present() -> None:
    runner = _FakeRunner(outputs={"docker": "Server: Docker Desktop\nOs: linux"})
    result = check_docker(run=runner, platform="darwin")
    assert result.status == "OK"


def test_check_docker_linux_ok_without_desktop_in_output() -> None:
    runner = _FakeRunner(outputs={"docker": "Server: Docker Engine"})
    result = check_docker(run=runner, platform="linux")
    assert result.status == "OK"


# ---------------------------------------------------------------------------
# check_git
# ---------------------------------------------------------------------------

def test_check_git_ok() -> None:
    runner = _FakeRunner(outputs={"git": "git version 2.43.0"})
    result = check_git(run=runner)
    assert result.status == "OK"
    assert "2.43.0" in result.message
    assert runner.calls[0] == ["git", "--version"]


def test_check_git_fail_not_installed() -> None:
    runner = _FakeRunner(failures={"git"})
    result = check_git(run=runner)
    assert result.status == "FAIL"
    assert "git" in result.message
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# check_tmux
# ---------------------------------------------------------------------------

def test_check_tmux_ok() -> None:
    runner = _FakeRunner(outputs={"tmux": "tmux 3.3a"})
    result = check_tmux(run=runner)
    assert result.status == "OK"
    assert "3.3a" in result.message
    assert runner.calls[0] == ["tmux", "-V"]


def test_check_tmux_fail_not_installed() -> None:
    runner = _FakeRunner(failures={"tmux"})
    result = check_tmux(run=runner)
    assert result.status == "FAIL"
    assert "tmux" in result.message
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# check_base_image
# ---------------------------------------------------------------------------

def test_check_base_image_ok() -> None:
    runner = _FakeRunner(outputs={"docker": '[{"Id": "sha256:abc"}]'})
    result = check_base_image(run=runner)
    assert result.status == "OK"
    assert runner.calls[0] == ["docker", "image", "inspect", "panopticon-base"]


def test_check_base_image_warn_not_built() -> None:
    # Not built is a non-fatal heads-up — the runner auto-builds it on first spawn.
    runner = _FakeRunner(failures={"docker"})
    result = check_base_image(run=runner)
    assert result.status == "WARN"
    assert "panopticon-base" in result.message
    # Remediation targets the pip-install command, not a Makefile target.
    assert "panopticon build" in (result.remediation or "")


def test_check_base_image_warn_called_process_error() -> None:
    class _FailRunner:
        def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
            raise subprocess.CalledProcessError(1, list(args))

    result = check_base_image(run=_FailRunner())
    assert result.status == "WARN"


# ---------------------------------------------------------------------------
# check_task_service
# ---------------------------------------------------------------------------

def test_check_task_service_ok() -> None:
    result = check_task_service(service_url="http://svc:8000", http_get=lambda _: 200)
    assert result.status == "OK"
    assert "svc:8000" in result.message


def test_check_task_service_warn_not_running() -> None:
    # Before the first `panopticon start` the service is down by design → WARN, not FAIL.
    result = check_task_service(service_url="http://svc:8000", http_get=lambda _: 0)
    assert result.status == "WARN"
    assert result.remediation is not None
    assert "panopticon start" in (result.remediation or "")


def test_check_task_service_warn_unexpected_status() -> None:
    result = check_task_service(service_url="http://svc:8000", http_get=lambda _: 503)
    assert result.status == "WARN"
    assert "503" in result.message


def test_check_task_service_probes_workflows_endpoint() -> None:
    probed: list[str] = []
    result = check_task_service(
        service_url="http://svc:8000",
        http_get=lambda url: (probed.append(url), 200)[1],
    )
    assert result.status == "OK"
    assert probed == ["http://svc:8000/workflows"]


# ---------------------------------------------------------------------------
# check_repo_env_file
# ---------------------------------------------------------------------------

_GOOD_OAUTH_TOKEN = "sk-ant-oat01-AAAA"
_GOOD_API_KEY = "sk-ant-api03-AAAA"


def _make_repo(env_file: str | None = "/sec/r1.env", name: str = "myrepo") -> dict[str, object]:
    return {"id": "r1", "name": name, "env_file": env_file}


def test_check_repo_env_file_no_env_file_configured() -> None:
    repo = _make_repo(env_file=None)
    results = check_repo_env_file(repo, read_file=lambda _: "", file_mode=lambda _: 0o600)
    assert results == []


def test_check_repo_env_file_ok_oauth_token() -> None:
    content = f"CLAUDE_CODE_OAUTH_TOKEN={_GOOD_OAUTH_TOKEN}\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    assert len(results) == 1
    assert results[0].status == "OK"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in results[0].message


def test_check_repo_env_file_ok_anthropic_api_key() -> None:
    content = f"ANTHROPIC_API_KEY={_GOOD_API_KEY}\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    assert len(results) == 1
    assert results[0].status == "OK"
    assert "ANTHROPIC_API_KEY" in results[0].message


def test_check_repo_env_file_fail_missing_file() -> None:
    def _raise(_: str) -> str:
        raise OSError("not found")

    results = check_repo_env_file(_make_repo(), read_file=_raise, file_mode=lambda _: 0o600)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "not found" in results[0].message


def test_check_repo_env_file_fail_group_or_other_access() -> None:
    content = f"CLAUDE_CODE_OAUTH_TOKEN={_GOOD_OAUTH_TOKEN}\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o644,
    )
    mode_fails = [r for r in results if r.status == "FAIL"]
    assert mode_fails, "expected a FAIL for group/other-readable env_file"
    assert "644" in mode_fails[0].message
    assert "chmod" in (mode_fails[0].remediation or "")


def test_check_repo_env_file_ok_stricter_owner_only_mode() -> None:
    # A stricter-than-0600 file (e.g. 0400, owner-read-only) has no group/other bits → not a FAIL.
    content = f"CLAUDE_CODE_OAUTH_TOKEN={_GOOD_OAUTH_TOKEN}\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o400,
    )
    assert [r for r in results if r.status == "FAIL"] == []
    assert results[-1].status == "OK"


def test_check_repo_env_file_fail_no_token() -> None:
    content = "OTHER_KEY=some_value\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    fails = [r for r in results if r.status == "FAIL"]
    assert fails
    assert "CLAUDE_CODE_OAUTH_TOKEN" in fails[0].message or "ANTHROPIC_API_KEY" in fails[0].message


def test_check_repo_env_file_fail_blank_token() -> None:
    content = "CLAUDE_CODE_OAUTH_TOKEN=\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    fails = [r for r in results if r.status == "FAIL"]
    assert fails


def test_check_repo_env_file_fail_bad_shape() -> None:
    content = "CLAUDE_CODE_OAUTH_TOKEN=wrong-prefix-token\n"
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    fails = [r for r in results if r.status == "FAIL"]
    assert fails
    assert "shape" in fails[0].message.lower() or "unrecognised" in fails[0].message.lower()


def test_check_repo_env_file_comments_and_blanks_ignored() -> None:
    content = (
        "# this is a comment\n"
        "\n"
        f"CLAUDE_CODE_OAUTH_TOKEN={_GOOD_OAUTH_TOKEN}\n"
    )
    results = check_repo_env_file(
        _make_repo(),
        read_file=lambda _: content,
        file_mode=lambda _: 0o600,
    )
    assert results[0].status == "OK"


# ---------------------------------------------------------------------------
# run_doctor — exit codes and repo-listing
# ---------------------------------------------------------------------------

def _run_doctor(
    *,
    runner: CommandRunner,
    list_repos: object = None,
    read_file: object = None,
    file_mode: object = None,
    platform: str = "linux",
    http_get: object = None,
) -> int:
    return run_doctor(
        service_url="http://svc:8000",
        list_repos=list_repos or (lambda: []),  # type: ignore[arg-type]
        run=runner,
        read_file=read_file or (lambda _: ""),  # type: ignore[arg-type]
        file_mode=file_mode or (lambda _: 0o600),  # type: ignore[arg-type]
        platform=platform,
        http_get=http_get or (lambda _: 200),  # type: ignore[arg-type]
    )


def test_run_doctor_returns_0_when_all_ok(capsys: pytest.CaptureFixture[str]) -> None:
    code = _run_doctor(runner=_healthy_runner())
    assert code == 0


def test_run_doctor_returns_1_when_any_fail(capsys: pytest.CaptureFixture[str]) -> None:
    code = _run_doctor(runner=_FakeRunner(failures={"docker"}))
    assert code == 1


def test_run_doctor_returns_0_with_only_warns(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _FakeRunner(outputs={
        "docker": "Server: Docker Engine",  # no "Desktop" on darwin → WARN
        "git": "git version 2.43.0",
        "tmux": "tmux 3.3a",
    })
    code = _run_doctor(runner=runner, platform="darwin")
    assert code == 0


def test_run_doctor_prints_fail_label(capsys: pytest.CaptureFixture[str]) -> None:
    _run_doctor(runner=_FakeRunner(failures={"tmux"}))
    out = capsys.readouterr().out
    assert "[FAIL]" in out


def test_run_doctor_prints_remediation(capsys: pytest.CaptureFixture[str]) -> None:
    _run_doctor(runner=_FakeRunner(failures={"tmux"}))
    out = capsys.readouterr().out
    assert "→" in out


def test_run_doctor_checks_repos(capsys: pytest.CaptureFixture[str]) -> None:
    repos = [{"id": "r1", "name": "myrepo", "env_file": "/sec/r1.env"}]
    code = _run_doctor(
        runner=_healthy_runner(),
        list_repos=lambda: repos,
        read_file=lambda _: f"CLAUDE_CODE_OAUTH_TOKEN={_GOOD_OAUTH_TOKEN}\n",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "myrepo" in out


class _NoBaseImageRunner:
    """Docker daemon up and git/tmux present, but the base image isn't built (inspect fails)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._outputs = {"docker": "Server: Docker Desktop", "git": "git version 2.43.0", "tmux": "tmux 3.3a"}

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        if list(args[:3]) == ["docker", "image", "inspect"]:
            raise FileNotFoundError("no such image")
        return self._outputs.get(args[0] if args else "", "")


def test_run_doctor_fresh_machine_before_first_start_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The headline case: a fresh host with Docker/git/tmux installed but nothing provisioned yet
    # (no base image, service not running). That's the expected pre-first-start state, so doctor
    # must exit 0 — only a missing host prerequisite is a failure.
    def _boom() -> list[dict[str, object]]:
        raise AssertionError("list_repos must not be called when the service is not running")

    code = _run_doctor(runner=_NoBaseImageRunner(), list_repos=_boom, http_get=lambda _: 0)  # type: ignore[arg-type]
    assert code == 0  # host prereqs OK; base image + service are non-fatal WARNs
    out = capsys.readouterr().out
    assert "not built yet" in out
    assert "not running" in out
    assert "Skipped per-repo token checks" in out
    assert "panopticon start" in out


def test_run_doctor_missing_prerequisite_fails(capsys: pytest.CaptureFixture[str]) -> None:
    # A genuinely missing host tool (git here) is the only thing that FAILs → exit 1.
    runner = _FakeRunner(outputs={"docker": "Server: Docker Desktop", "tmux": "tmux 3.3a"}, failures={"git"})
    code = _run_doctor(runner=runner, http_get=lambda _: 0)
    assert code == 1
    out = capsys.readouterr().out
    assert "prerequisite check(s) FAILED" in out


def test_run_doctor_survives_list_repos_error_when_service_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Service says OK but the repos call still errors (race, transient) → WARN, no crash.
    def _boom() -> list[dict[str, object]]:
        raise RuntimeError("connection reset")

    code = _run_doctor(runner=_healthy_runner(), list_repos=_boom, http_get=lambda _: 200)
    assert code == 0  # only a WARN, no FAIL
    out = capsys.readouterr().out
    assert "Could not list repos" in out


def test_run_doctor_emits_expected_host_tool_commands() -> None:
    runner = _healthy_runner()
    _run_doctor(runner=runner)
    assert ["docker", "info"] in runner.calls
    assert ["git", "--version"] in runner.calls
    assert ["tmux", "-V"] in runner.calls
    assert ["docker", "image", "inspect", "panopticon-base"] in runner.calls
