"""The tarot review-artifact gate — a `PreToolUse` hook on `apply_operation` (advance).

For a repo that opts in (`Repo.capabilities["tarot_review"]`, see
:mod:`panopticon.workflows.github_forge`), every other ITERATING responsibility is agent
self-attested, but this one is real-verified: this hook intercepts the `advance` operation while
the task is in ITERATING and runs `tarot strands check` / `tarot tour check` in `/workspace`,
denying the tool call (with the checks' output as the reason, so it lands in the agent's context
like a failed test would) unless they pass. A trivial diff (below a changed-line threshold) skips
the checks and auto-resolves the responsibility instead — no tour to write for a one-line fix.

Registered unconditionally in `container/hooks.py` (like the turn-flip hooks), regardless of
workflow — irrelevant calls (a different operation, a non-ITERATING state, a non-opted-in repo)
resolve in the first couple of checks below and allow immediately. Deterministic and LLM-free:
only subprocess + REST calls, so it's unit-tested with a fake command runner and a fake client
(no real `tarot` binary needed), the same shape as :mod:`panopticon.container.hook`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.models import Status

#: The `Repo.capabilities` key that opts a repo into this gate (mirrors `docker_in_docker`).
TAROT_REVIEW_CAPABILITY = "tarot_review"
#: Optional per-repo override (an int) of the trivial-diff line threshold, under `capabilities`.
TAROT_REVIEW_THRESHOLD_CAPABILITY = "tarot_review_threshold"
#: The ITERATING responsibility this gate verifies (see `GithubForgeWorkflow.TAROT_REVIEW_ARTIFACTS`).
RESPONSIBILITY_KEY = "tarot-review-artifacts"
#: Below this many total changed lines (git `diff --numstat`, added + removed), the diff is
#: considered trivial and the tarot checks are skipped entirely.
DEFAULT_TRIVIAL_THRESHOLD = 20
WORKSPACE = "/workspace"


@dataclass(frozen=True)
class CommandResult:
    """The outcome of running one external command — never raises; the caller inspects it."""

    returncode: int
    output: str  # combined stdout+stderr
    found: bool = True  # False when the executable itself wasn't found on PATH


class CommandRunner(Protocol):
    def __call__(self, args: Sequence[str], *, cwd: str | None = None) -> CommandResult: ...


def _subprocess_run(args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
    try:
        proc = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return CommandResult(returncode=127, output=f"{args[0]}: command not found", found=False)
    return CommandResult(returncode=proc.returncode, output=proc.stdout + proc.stderr)


def _read_payload(stdin: TextIO) -> dict[str, Any]:
    """Tolerantly parse the hook's stdin JSON; empty/invalid input yields an empty payload."""
    try:
        raw = stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _allow() -> int:
    """No stdout, exit 0 — Claude Code lets the tool call through unmodified."""
    return 0


def _deny(reason: str) -> int:
    """Structured `PreToolUse` denial: the reason string lands in the agent's context as the
    tool call's failure, the same seam a failed test's output would use."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def _opted_in(repo: JsonObj) -> bool:
    return bool((repo.get("capabilities") or {}).get(TAROT_REVIEW_CAPABILITY))


def _threshold(repo: JsonObj) -> int:
    value = (repo.get("capabilities") or {}).get(TAROT_REVIEW_THRESHOLD_CAPABILITY)
    return value if isinstance(value, int) else DEFAULT_TRIVIAL_THRESHOLD


def _changed_line_count(run: CommandRunner, *, base_ref: str) -> int:
    """Total added+removed lines between ``base_ref`` and ``HEAD`` (a `diff --numstat` sum) — the
    trivial-diff heuristic. Binary files report ``-`` counts, which don't parse as digits and are
    skipped rather than counted."""
    result = run(["git", "-C", WORKSPACE, "diff", "--numstat", f"{base_ref}...HEAD"])
    total = 0
    for line in result.output.splitlines():
        added, _, rest = line.partition("\t")
        removed, _, _path = rest.partition("\t")
        for count in (added, removed):
            if count.isdigit():
                total += int(count)
    return total


def _run_tarot_checks(run: CommandRunner) -> CommandResult | None:
    """Run `tarot strands check` then `tarot tour check`; stop at the first failure (nothing to
    gain running both once one has already failed). ``None`` means both passed."""
    for args in (["tarot", "strands", "check"], ["tarot", "tour", "check"]):
        result = run(args, cwd=WORKSPACE)
        if result.returncode != 0:
            return result
    return None


def main(
    *,
    client: TaskServiceClient | None = None,
    stdin: TextIO | None = None,
    run: CommandRunner = _subprocess_run,
) -> int:
    payload = _read_payload(stdin or sys.stdin)
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("operation") != "advance":
        return _allow()

    env = os.environ
    task_id = env["PANOPTICON_TASK_ID"]
    client = client or TaskServiceClient(httpx.Client(base_url=env["PANOPTICON_SERVICE_URL"]))

    task = client.get_task(task_id)
    if task.get("state") != "ITERATING":
        return _allow()

    repo = client.get_repo(task["repo_id"])
    if not _opted_in(repo):
        return _allow()

    base_ref = f"origin/{repo.get('default_base', 'main')}"
    if _changed_line_count(run, base_ref=base_ref) < _threshold(repo):
        client.resolve_responsibility(
            task_id,
            RESPONSIBILITY_KEY,
            Status.MET,
            comment="trivial diff — tarot review skipped",
        )
        return _allow()

    failure = _run_tarot_checks(run)
    if failure is None:
        client.resolve_responsibility(
            task_id,
            RESPONSIBILITY_KEY,
            Status.MET,
            comment="verified by tarot strands check / tarot tour check",
        )
        return _allow()
    if not failure.found:
        return _deny(
            "`tarot` is not installed in this container. An opted-in repo must install it via "
            "its `image_layer_file` (see docs/repos.md)."
        )
    return _deny(failure.output)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
