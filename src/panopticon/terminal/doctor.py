"""``panopticon doctor`` — preflight self-check, meant to run *before* the first ``panopticon
start`` on a fresh pip-installed host.

It separates two kinds of check:

* **Host prerequisites** — Docker, git, tmux — external tools you must install yourself. A
  missing one is a hard **FAIL** (doctor exits non-zero): panopticon can't run without it.
* **Setup status** — the ``panopticon-base`` image and the task service — things panopticon
  provisions for you. Before first start neither exists yet, so their absence is a non-fatal
  **WARN** heads-up (the base image is auto-built on first spawn; the service is started by
  ``panopticon start``), never a FAIL. When the service *is* up, per-repo env-file tokens are
  checked too.

So a fresh machine with Docker/git/tmux installed passes (exit 0) with a short "here's what
happens at first start" list, and only a genuinely missing prerequisite fails. Remediations are
phrased for a pip install (``panopticon build`` / ``panopticon start``, not ``make`` targets).
Injectable command-runner and filesystem callables for testability. LLM-free.
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


def check_git(*, run: CommandRunner) -> CheckResult:
    """Verify git is installed — the session service clones each task's checkout on the host."""
    try:
        version = run(["git", "--version"]).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="FAIL",
            name="git",
            message="git not found",
            remediation="Install git: brew install git (macOS) or apt-get install --yes git (Linux)",
        )
    return CheckResult(status="OK", name="git", message=version or "git present")


def check_tmux(*, run: CommandRunner) -> CheckResult:
    """Verify tmux is installed — ``panopticon start`` runs services and task panes under tmux."""
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


def check_base_image(*, run: CommandRunner) -> CheckResult:
    """Report whether the panopticon-base image is built.

    Missing is **not** a failure: the runner builds it automatically on first spawn
    (``ImageBuilder.build_base_if_missing``), so before first start it's simply not there yet.
    A WARN heads-up, so an operator who wants to pre-build knows the command.
    """
    try:
        run(["docker", "image", "inspect", "panopticon-base"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return CheckResult(
            status="WARN",
            name="Base image",
            message="Base image panopticon-base not built yet",
            remediation="Pre-build with 'panopticon build', or let the first task build it automatically",
        )
    return CheckResult(status="OK", name="Base image", message="panopticon-base present")


def check_task_service(*, service_url: str, http_get: Callable[[str], int]) -> CheckResult:
    """Report whether the task service is running.

    Not running is **expected** before the first ``panopticon start`` — a WARN heads-up, not a
    failure. (An HTTP error from a service that *is* answering is also a non-fatal WARN.)
    """
    url = service_url.rstrip("/") + "/workflows"
    status_code = http_get(url)
    if status_code == 0:
        return CheckResult(
            status="WARN",
            name="Task service",
            message=f"Task service not running at {service_url} (expected before first start)",
            remediation="Start it with: panopticon start (or panopticon host)",
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
    """Check a repo's env_file: present, owner-only mode, contains a recognisable auth token."""
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
        if mode & 0o077:  # any group/other access — the file carries secrets
            results.append(CheckResult(
                status="FAIL",
                name=check_name,
                message=f"env_file mode is {oct(mode)}, expected owner-only (no group/other access): {env_file}",
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
    *,
    service_url: str,
    list_repos: Callable[[], list[JsonObj]],
    run: CommandRunner = _subprocess_run,
    read_file: Callable[[str], str] = _default_read_file,
    file_mode: Callable[[str], int] = _default_file_mode,
    platform: str = sys.platform,
    http_get: Callable[[str], int] = _default_http_get,
) -> int:
    """Run all preflight checks, print results, return 0 (all OK/WARN) or 1 (any FAIL).

    ``list_repos`` is called **lazily** — only after the task service is confirmed reachable —
    so ``doctor`` still runs (and reports the service as not-yet-running) when it's offline,
    rather than crashing on an eager fetch. Before the first ``panopticon start`` the service is
    down by definition, so that offline case is doctor's headline use.

    Only host-prerequisite FAILs (Docker/git/tmux) set a non-zero exit; a not-yet-provisioned
    base image or task service is a WARN, so a fresh-but-ready machine exits 0.
    """
    results: list[CheckResult] = [
        check_docker(run=run, platform=platform),
        check_git(run=run),
        check_tmux(run=run),
        check_base_image(run=run),
    ]
    service_result = check_task_service(service_url=service_url, http_get=http_get)
    results.append(service_result)

    if service_result.status == "OK":
        try:
            repos = list_repos()
        except Exception as exc:
            results.append(CheckResult(
                status="WARN",
                name="Repos",
                message=f"Could not list repos: {exc}",
            ))
            repos = []
        for repo in repos:
            results.extend(check_repo_env_file(repo, read_file=read_file, file_mode=file_mode))
    else:
        results.append(CheckResult(
            status="WARN",
            name="Repos",
            message="Skipped per-repo token checks — task service not running yet",
        ))

    _STATUS_LABEL: dict[CheckStatus, str] = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for r in results:
        label = _STATUS_LABEL[r.status]
        prefix = f"{r.name}: " if r.name not in r.message else ""
        print(f"{label} {prefix}{r.message}")
        if r.remediation:
            print(f"       → {r.remediation}")

    fails = sum(1 for r in results if r.status == "FAIL")
    if fails:
        print(f"\n{fails} prerequisite check(s) FAILED — install the missing tool(s) above, then re-run.")
        return 1
    warns = sum(1 for r in results if r.status == "WARN")
    if warns:
        print(f"\nHost prerequisites OK. {warns} item(s) not provisioned yet — expected before your first 'panopticon start'.")
    else:
        print("\nAll checks passed.")
    return 0
