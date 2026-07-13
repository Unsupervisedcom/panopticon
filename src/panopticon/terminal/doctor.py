"""``panopticon doctor`` — check the host prerequisites for the pip-installed operator.

Verifies the **required host binaries** the operator's flows shell out to before anything is
spawned — enough to run ``panopticon quickstart``, ``panopticon start`` and the ``setup-repo``
token flow — plus that the docker daemon is actually reachable (a present client with a dead
daemon fails every spawn). It prints a ``✓``/``✗`` line per check and returns a non-zero exit
code when a required prerequisite is missing, so a fresh install can self-diagnose.

Deliberately scoped to *required binaries* — it does not inspect credential/config readiness
(the secrets env-file, ``CLAUDE_CODE_OAUTH_TOKEN``, ``GH_TOKEN``), the task-service port, or the
``panopticon-base`` image (the spawn path auto-builds it). Dev tooling (``uv``/``make``) is not a
prerequisite for a pip install.

Pure and injectable (the binary probe, the command runner and the interpreter version are all
parameters) so it's unit-testable without touching the real host.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: The minimum Python the package supports (``pyproject`` ``requires-python``). The CLI re-execs
#: ``sys.executable`` for the background services, so the running interpreter must satisfy it.
MIN_PYTHON = (3, 11)

#: Probe a binary on ``PATH``; returns its path or ``None`` (``shutil.which``'s contract).
Which = Callable[[str], "str | None"]

#: Run a command and return its exit status (no output captured for the caller).
Run = Callable[[Sequence[str]], int]


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one prerequisite check.

    ``hint`` is a remediation string shown only when the check fails (``ok`` is ``False``).
    """

    name: str
    ok: bool
    detail: str
    hint: str = ""


#: Required host binaries and how to install each, in the order they're reported. Each is
#: something one of the operator's flows shells out to on the host:
#:   git    — quickstart's ``git remote get-url``; the per-task ``git clone --local`` + branch
#:   docker — the session service builds images and ``docker run``s every task container
#:   tmux   — ``start``/``host`` background sessions, the console attach, shell workflows
#:   claude — ``setup-repo`` runs ``claude setup-token`` on the host (not in a container)
REQUIRED_BINARIES: tuple[tuple[str, str], ...] = (
    ("git", "Install git (e.g. `brew install git` / `apt-get install --yes git`)."),
    (
        "docker",
        "Install Docker Desktop (macOS) or Docker Engine (Linux), then start it.",
    ),
    ("tmux", "Install tmux (e.g. `brew install tmux` / `apt-get install --yes tmux`)."),
    (
        "claude",
        "Install the Claude Code CLI (see docs.claude.com/claude-code) — needed for "
        "`claude setup-token` during setup-repo.",
    ),
)


def _subprocess_status(command: Sequence[str]) -> int:
    """Default :data:`Run`: run ``command`` quietly and return its exit status.

    Output is captured (and discarded) so a probe like ``docker info`` never spams the terminal;
    a missing binary surfaces as a non-zero status rather than an exception.
    """
    try:
        return subprocess.run(list(command), capture_output=True).returncode
    except OSError:
        return 127


def check_python(version_info: object = sys.version_info) -> CheckResult:
    """Check the running interpreter satisfies :data:`MIN_PYTHON`."""
    version = tuple(version_info[:2])  # type: ignore[index]
    shown = ".".join(str(part) for part in version)
    wanted = ".".join(str(part) for part in MIN_PYTHON)
    if version >= MIN_PYTHON:
        return CheckResult("python", True, f"{shown} (>= {wanted})")
    return CheckResult(
        "python",
        False,
        f"{shown} is older than the required {wanted}",
        hint=f"Run panopticon under Python {wanted} or newer.",
    )


def check_binary(name: str, *, hint: str, which: Which = shutil.which) -> CheckResult:
    """Check ``name`` resolves on ``PATH``."""
    path = which(name)
    if path:
        return CheckResult(name, True, f"found at {path}")
    return CheckResult(name, False, "not found on PATH", hint=hint)


def check_docker_daemon(run: Run = _subprocess_status) -> CheckResult:
    """Check the docker daemon is reachable via ``docker info``.

    Only meaningful once the ``docker`` binary is present — :func:`run_checks` gates on that.
    """
    if run(["docker", "info"]) == 0:
        return CheckResult("docker daemon", True, "reachable")
    return CheckResult(
        "docker daemon",
        False,
        "docker is installed but its daemon isn't reachable",
        hint="Start Docker Desktop, or `systemctl start docker` / `open -a Docker`.",
    )


def run_checks(
    *,
    which: Which = shutil.which,
    run: Run = _subprocess_status,
    version_info: object = sys.version_info,
) -> list[CheckResult]:
    """Run every required-prerequisite check and return the results in report order.

    The docker-daemon check is appended **only when the docker binary is present**, so a host
    without docker reports one clear failure (the missing binary) rather than two.
    """
    results = [check_python(version_info)]
    docker_present = False
    for name, hint in REQUIRED_BINARIES:
        result = check_binary(name, hint=hint, which=which)
        results.append(result)
        if name == "docker":
            docker_present = result.ok
    if docker_present:
        results.append(check_docker_daemon(run))
    return results


def render(results: Sequence[CheckResult]) -> str:
    """Render ``results`` as a human-readable report: one line per check, then a summary."""
    lines = ["Checking host prerequisites for panopticon quickstart / start / setup-repo:", ""]
    for result in results:
        mark = "✓" if result.ok else "✗"
        lines.append(f"  {mark} {result.name}: {result.detail}")
        if not result.ok and result.hint:
            lines.append(f"      → {result.hint}")
    failures = [result for result in results if not result.ok]
    lines.append("")
    if failures:
        missing = ", ".join(result.name for result in failures)
        lines.append(f"{len(failures)} prerequisite(s) missing: {missing}.")
    else:
        lines.append("All prerequisites satisfied.")
    return "\n".join(lines)


def report(results: Sequence[CheckResult]) -> int:
    """Print :func:`render` for ``results`` and return an exit code (1 if any check failed)."""
    print(render(results))
    return 1 if any(not result.ok for result in results) else 0
