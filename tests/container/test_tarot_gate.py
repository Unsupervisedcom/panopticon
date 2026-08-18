"""The tarot review-artifact gate: a `PreToolUse` hook on `apply_operation` (advance).

Pins every branch: irrelevant calls (wrong operation, wrong state, non-opted-in repo) allow
immediately with no side effects; a trivial diff auto-resolves the responsibility without
running the tarot CLIs; a non-trivial diff runs them and either auto-resolves (both pass) or
denies with the captured output (either fails); a missing `tarot` binary denies with an
operator-facing message. Deterministic and LLM-free — a fake client and a fake command runner,
no real `tarot`/`git` needed (the same style as :mod:`tests.container.test_hooks`).
"""

from __future__ import annotations

import io
import json

from panopticon.client import JsonObj
from panopticon.container import tarot_gate
from panopticon.container.tarot_gate import CommandResult

WORKSPACE = "/workspace"


class _FakeClient:
    def __init__(self, task: JsonObj, repo: JsonObj) -> None:
        self._task = task
        self._repo = repo
        self.resolved: list[tuple[str, str, object, str | None]] = []

    def get_task(self, task_id: str) -> JsonObj:
        assert task_id == self._task["id"]
        return self._task

    def get_repo(self, repo_id: str) -> JsonObj:
        assert repo_id == self._repo["id"]
        return self._repo

    def resolve_responsibility(
        self, task_id: str, key: str, status: object, comment: str | None = None
    ) -> JsonObj:
        self.resolved.append((task_id, key, status, comment))
        return {}


class _FakeRun:
    """Records every command it's asked to run; returns a canned result per exact argv."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, args: list[str], *, cwd: str | None = None) -> CommandResult:
        key = tuple(args)
        self.calls.append((key, cwd))
        return self._responses.get(key, CommandResult(returncode=0, output=""))


def _task(state: str = "ITERATING") -> JsonObj:
    return {"id": "t1", "state": state, "repo_id": "r1"}


def _repo(*, opted_in: bool, threshold: int | None = None, default_base: str = "main") -> JsonObj:
    capabilities: dict[str, object] = {"tarot_review": opted_in}
    if threshold is not None:
        capabilities["tarot_review_threshold"] = threshold
    return {"id": "r1", "default_base": default_base, "capabilities": capabilities}


def _numstat_key(base_ref: str) -> tuple[str, ...]:
    return ("git", "-C", WORKSPACE, "diff", "--numstat", f"{base_ref}...HEAD")


def _payload(operation: str | None) -> str:
    tool_input: JsonObj = {} if operation is None else {"operation": operation}
    return json.dumps({"tool_name": "mcp__panopticon__apply_operation", "tool_input": tool_input})


def _run_gate(
    *, client: _FakeClient, run: _FakeRun, operation: str = "advance", env: dict[str, str]
) -> tuple[int, str]:
    import os

    old = dict(os.environ)
    os.environ.update(env)
    try:
        stdin = io.StringIO(_payload(operation))
        code = tarot_gate.main(client=client, stdin=stdin, run=run)  # type: ignore[arg-type]
    finally:
        os.environ.clear()
        os.environ.update(old)
    return code, ""


ENV = {"PANOPTICON_TASK_ID": "t1", "PANOPTICON_SERVICE_URL": "http://svc"}


def _stdout(capsys) -> str:  # type: ignore[no-untyped-def]
    return capsys.readouterr().out


# -- irrelevant calls allow immediately, no side effects -----------------------------


def test_non_advance_operation_allows_with_no_client_calls() -> None:
    stdin = io.StringIO(_payload("drop"))
    # No env vars set at all — a non-advance operation never even reads them.
    assert tarot_gate.main(stdin=stdin, run=_FakeRun()) == 0  # type: ignore[arg-type]


def test_missing_operation_field_allows() -> None:
    stdin = io.StringIO(_payload(None))
    assert tarot_gate.main(stdin=stdin, run=_FakeRun()) == 0  # type: ignore[arg-type]


def test_non_iterating_state_allows(capsys) -> None:  # type: ignore[no-untyped-def]
    client = _FakeClient(_task(state="PLANNING"), _repo(opted_in=True))
    run = _FakeRun()
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert _stdout(capsys) == ""
    assert run.calls == []  # never even checked the diff
    assert client.resolved == []


def test_non_opted_in_repo_allows(capsys) -> None:  # type: ignore[no-untyped-def]
    client = _FakeClient(_task(), _repo(opted_in=False))
    run = _FakeRun()
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert _stdout(capsys) == ""
    assert run.calls == []
    assert client.resolved == []


# -- trivial diff: auto-resolve without running the tarot CLIs ----------------------


def test_trivial_diff_auto_resolves_and_skips_tarot_checks(capsys) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(opted_in=True)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {_numstat_key("origin/main"): CommandResult(returncode=0, output="3\t2\tfoo.py\n")}
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert _stdout(capsys) == ""
    assert client.resolved == [
        (
            "t1",
            "tarot-review-artifacts",
            tarot_gate.Status.MET,
            "trivial diff — tarot review skipped",
        )
    ]
    assert ("tarot", "strands", "check") not in {c[0] for c in run.calls}


def test_trivial_diff_threshold_is_overridable_per_repo(capsys) -> None:  # type: ignore[no-untyped-def]
    # 25 changed lines is above the default (20) but below a repo override of 30.
    repo = _repo(opted_in=True, threshold=30)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {_numstat_key("origin/main"): CommandResult(returncode=0, output="20\t5\tfoo.py\n")}
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert client.resolved and client.resolved[0][3] == "trivial diff — tarot review skipped"


# -- non-trivial diff: run the tarot CLIs --------------------------------------------


def _big_diff() -> CommandResult:
    return CommandResult(returncode=0, output="500\t20\tbig.py\n")


def test_non_trivial_diff_both_checks_pass_auto_resolves(capsys) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(opted_in=True)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {
            _numstat_key("origin/main"): _big_diff(),
            ("tarot", "strands", "check"): CommandResult(returncode=0, output="ok"),
            ("tarot", "tour", "check"): CommandResult(returncode=0, output="ok"),
        }
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert _stdout(capsys) == ""
    assert client.resolved == [
        (
            "t1",
            "tarot-review-artifacts",
            tarot_gate.Status.MET,
            "verified by tarot strands check / tarot tour check",
        )
    ]


def test_strands_check_failure_denies_with_output_and_leaves_pending(capsys) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(opted_in=True)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {
            _numstat_key("origin/main"): _big_diff(),
            ("tarot", "strands", "check"): CommandResult(
                returncode=1, output="strands.json missing a strand for foo()"
            ),
        }
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0  # denial is exit 0 + JSON, per the PreToolUse contract
    out = json.loads(_stdout(capsys))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "strands.json missing a strand for foo()" in reason
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert client.resolved == []  # left PENDING — never optimistically resolved
    # tour check never ran — strands already failed
    assert ("tarot", "tour", "check") not in {c[0] for c in run.calls}


def test_tour_check_failure_denies_with_its_output(capsys) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(opted_in=True)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {
            _numstat_key("origin/main"): _big_diff(),
            ("tarot", "strands", "check"): CommandResult(returncode=0, output="ok"),
            ("tarot", "tour", "check"): CommandResult(returncode=1, output="tour missing a stop"),
        }
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    out = json.loads(_stdout(capsys))
    assert "tour missing a stop" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert client.resolved == []


def test_missing_tarot_binary_denies_with_operator_message(capsys) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(opted_in=True)
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {
            _numstat_key("origin/main"): _big_diff(),
            ("tarot", "strands", "check"): CommandResult(
                returncode=127, output="tarot: command not found", found=False
            ),
        }
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    out = json.loads(_stdout(capsys))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "not installed" in reason and "image_layer_file" in reason
    assert client.resolved == []


def test_default_base_branch_is_used_for_the_diff_base() -> None:
    repo = _repo(opted_in=True, default_base="develop")
    client = _FakeClient(_task(), repo)
    run = _FakeRun(
        {_numstat_key("origin/develop"): CommandResult(returncode=0, output="1\t1\tx\n")}
    )
    code, _ = _run_gate(client=client, run=run, env=ENV)
    assert code == 0
    assert client.resolved  # matched the develop-based numstat key, so it ran and resolved
