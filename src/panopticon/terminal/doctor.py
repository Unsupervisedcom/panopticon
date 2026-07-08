"""``panopticon doctor`` — preflight self-check for a panopticon operator environment.

Verifies Docker, tmux, uv, the base task-container image, the task service, and per-repo
token configuration. Prints OK / WARN / FAIL with a one-line remediation per check; exits
non-zero if any check FAILs. Injectable command-runner and filesystem callables for
testability. LLM-free.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from panopticon.client import JsonObj

#: Recognised auth-token prefixes per key name.
_TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "CLAUDE_CODE_OAUTH_TOKEN": re.compile(r"^sk-ant-oat01-"),
    "ANTHROPIC_API_KEY": re.compile(r"^sk-ant-api03-"),
}

CheckStatus = Literal["OK", "WARN", "FAIL"]


@dataclass
class CheckResult:
    status: CheckStatus
    name: str
    message: str
    remediation: str | None = None


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on non-zero exit."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def _default_read_file(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _default_file_mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _default_http_get(url: str) -> int:
    """Return the HTTP status code, or 0 when the service is unreachable."""
    try:
        return httpx.get(url, timeout=5.0).status_code
    except Exception:
        return 0


def check_docker(*, run: CommandRunner, platform: str = sys.platform) -> CheckResult:
    """Verify the Docker daemon is reachable; on macOS warn if Docker Desktop looks absent."""
    try:
        output = run(["docker", "info"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="FAIL",
            name="Docker daemon",
            message="Docker daemon not reachable",
            remediation="Start Docker Desktop (macOS/Windows) or the Docker daemon (Linux)",
        )
    if platform == "darwin" and "Desktop" not in output:
        return CheckResult(
            status="WARN",
            name="Docker daemon",
            message="Docker daemon reachable — Docker Desktop not detected on macOS",
            remediation=(
                "Install Docker Desktop — containers reach the host task service via "
                "host.docker.internal, which only Docker Desktop provides on macOS"
            ),
        )
    return CheckResult(status="OK", name="Docker daemon", message="Docker daemon reachable")


def check_tmux(*, run: CommandRunner) -> CheckResult:
    """Verify tmux is installed."""
    try:
        version = run(["tmux", "-V"]).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="FAIL",
            name="tmux",
            message="tmux not found",
            remediation="Install tmux: brew install tmux (macOS) or apt-get install --yes tmux (Linux)",
        )
    return CheckResult(status="OK", name="tmux", message=version or "tmux present")


def check_uv(*, run: CommandRunner) -> CheckResult:
    """Verify uv is installed."""
    try:
        version = run(["uv", "--version"]).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="FAIL",
            name="uv",
            message="uv not found",
            remediation="Install uv: brew install uv (macOS) or pip install uv, then run: make sync",
        )
    return CheckResult(status="OK", name="uv", message=version or "uv present")


def check_base_image(*, run: CommandRunner) -> CheckResult:
    """Verify the panopticon-base Docker image is present."""
    try:
        run(["docker", "image", "inspect", "panopticon-base"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="FAIL",
            name="Base image",
            message="Base image panopticon-base not found",
            remediation="Run: make build",
        )
    return CheckResult(status="OK", name="Base image", message="panopticon-base present")


def check_task_service(*, service_url: str, http_get: Callable[[str], int]) -> CheckResult:
    """Verify the task service is reachable."""
    url = service_url.rstrip("/") + "/workflows"
    status_code = http_get(url)
    if status_code == 0:
        return CheckResult(
            status="FAIL",
            name="Task service",
            message=f"Task service not reachable at {service_url}",
            remediation="Run: make serve (or make start)",
        )
    if status_code != 200:
        return CheckResult(
            status="WARN",
            name="Task service",
            message=f"Task service returned HTTP {status_code} at {service_url}",
        )
    return CheckResult(
        status="OK",
        name="Task service",
        message=f"Task service reachable at {service_url}",
    )


def check_repo_env_file(
    repo: JsonObj,
    *,
    read_file: Callable[[str], str],
    file_mode: Callable[[str], int],
) -> list[CheckResult]:
    """Check a repo's env_file: present, mode 0600, contains a recognisable auth token."""
    env_file: str | None = repo.get("env_file")
    if not env_file:
        return []  # no env_file configured — skip

    repo_label = repo.get("name") or repo.get("id") or "unknown"
    check_name = f"Repo '{repo_label}' env_file"
    results: list[CheckResult] = []

    try:
        content = read_file(env_file)
    except OSError:
        return [CheckResult(
            status="FAIL",
            name=check_name,
            message=f"env_file not found: {env_file}",
            remediation=f"Create {env_file} with CLAUDE_CODE_OAUTH_TOKEN=<your-token>",
        )]

    try:
        mode = file_mode(env_file)
        if mode != 0o600:
            results.append(CheckResult(
                status="FAIL",
                name=check_name,
                message=f"env_file mode is {oct(mode)}, expected 0o600: {env_file}",
                remediation=f"Run: chmod 600 {env_file}",
            ))
    except OSError:
        pass

    tokens: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            tokens[key.strip()] = value.strip()

    good: list[str] = []
    bad_shape: list[str] = []
    for key, pattern in _TOKEN_PATTERNS.items():
        value = tokens.get(key, "")
        if value:
            if pattern.match(value):
                good.append(key)
            else:
                bad_shape.append(key)

    if good:
        results.append(CheckResult(
            status="OK",
            name=check_name,
            message=f"{good[0]} present and well-formed in {env_file}",
        ))
    elif bad_shape:
        for key in bad_shape:
            results.append(CheckResult(
                status="FAIL",
                name=check_name,
                message=f"{key} has an unrecognised shape in {env_file}",
                remediation=f"Expected prefix {_TOKEN_PATTERNS[key].pattern!r} — regenerate your token",
            ))
    else:
        results.append(CheckResult(
            status="FAIL",
            name=check_name,
            message=f"No CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY found in {env_file}",
            remediation=f"Add CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-... to {env_file}",
        ))

    return results


def run_doctor(
    repos: list[JsonObj],
    *,
    service_url: str,
    run: CommandRunner = _subprocess_run,
    read_file: Callable[[str], str] = _default_read_file,
    file_mode: Callable[[str], int] = _default_file_mode,
    platform: str = sys.platform,
    http_get: Callable[[str], int] = _default_http_get,
) -> int:
    """Run all preflight checks, print results, return 0 (all OK/WARN) or 1 (any FAIL)."""
    results: list[CheckResult] = [
        check_docker(run=run, platform=platform),
        check_tmux(run=run),
        check_uv(run=run),
        check_base_image(run=run),
        check_task_service(service_url=service_url, http_get=http_get),
    ]
    for repo in repos:
        results.extend(check_repo_env_file(repo, read_file=read_file, file_mode=file_mode))

    _STATUS_LABEL: dict[CheckStatus, str] = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for r in results:
        label = _STATUS_LABEL[r.status]
        prefix = f"{r.name}: " if r.name not in r.message else ""
        print(f"{label} {prefix}{r.message}")
        if r.remediation:
            print(f"       → {r.remediation}")

    fails = sum(1 for r in results if r.status == "FAIL")
    if fails:
        print(f"\n{fails} check(s) FAILED.")
        return 1
    warns = sum(1 for r in results if r.status == "WARN")
    if warns:
        print(f"\n{warns} check(s) have warnings.")
    else:
        print("\nAll checks passed.")
    return 0
