"""The host-side tarot review-artifact gate — the golden spec for the one *verified* responsibility.

Drives the real :class:`TaskService` with a fake command-runner, so no real `tarot` and no real
repo on disk are needed. What matters here is policy: when the gate runs at all, what it refuses,
what it records on the task, and — crucially — that a refusal leaves the task exactly where it was.

One test at the bottom goes through the *real* subprocess runner against a stub `tarot` script, so
the exit-code contract is pinned for real and not only through fakes.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from panopticon.core.models import Repo, Status
from panopticon.core.tarot import CommandResult, TarotCLI
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.tarot_gate import (
    DEFAULT_TRIVIAL_THRESHOLD,
    RESPONSIBILITY_KEY,
    TarotGate,
    TarotGateRefused,
)
from panopticon.workflows import GithubPeerReviewed, Spike

CLONE = "/tasks/t1"

#: Every ITERATING responsibility the agent self-attests, so only the tarot one is left to the gate.
SELF_ATTESTED = (
    "plan-implemented",
    "requests-implemented",
    "tests-pass",
    "committed-pushed",
    "ci-passing",
    "pr-updated",
    "url-recorded",
)


class FakeRun:
    """Records argv; returns a canned result per matched prefix (default: success, empty output)."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        for prefix, result in self._responses.items():
            if argv[: len(prefix)] == prefix:
                return result
        return CommandResult(returncode=0, output="")

    @property
    def tarot_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c and c[0].endswith("tarot")]


def make_gate(
    run: FakeRun, *, binary: str | None = "tarot", clone_exists: bool = True
) -> TarotGate:
    return TarotGate(
        cli=TarotCLI(run=run, tarot_binary=binary),
        run=run,
        clone_exists=lambda path: clone_exists,
    )


async def make_service(
    tmp_path: Path,
    *,
    gate: TarotGate,
    opted_in: bool = True,
    capabilities: dict[str, object] | None = None,
) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 10_000))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 10_000))
    svc = TaskService(
        SqlAlchemyStore(),
        {"github-peer-reviewed": GithubPeerReviewed()},
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
        tarot_gate=gate,
    )
    await svc.init()
    caps: dict[str, object] = dict(capabilities or {})
    if opted_in:
        caps.setdefault("tarot_review", True)
    await svc.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://example.invalid/r1.git",
            default_base="main",
            enabled_workflows=["github-peer-reviewed"],
            capabilities=caps,
        )
    )
    return svc


async def iterating_task(svc: TaskService, *, clone: str | None = CLONE) -> str:
    """A task parked in ITERATING with every self-attested responsibility already resolved."""
    task = await svc.create_task("r1", "github-peer-reviewed")
    await svc.set_slug(task.id, "a-task")
    if clone is not None:
        await svc.record_provisioning(task.id, branch="panopticon/a-task", clone=clone)
    for key in ("plan-written", "token-estimated"):
        await svc.resolve_responsibility(task.id, key, status=Status.MET, comment="done")
    await svc.apply_operation(task.id, "advance")  # PLANNING → ITERATING
    for key in SELF_ATTESTED:
        await svc.resolve_responsibility(task.id, key, status=Status.MET, comment="done")
    return task.id


async def responsibility(svc: TaskService, task_id: str):
    task = await svc.get_task(task_id)
    return next(
        (r for r in task.current_entry.responsibilities if r.key == RESPONSIBILITY_KEY), None
    )


# -- the happy path ---------------------------------------------------------------


async def test_passing_checks_allow_the_advance_and_record_met(tmp_path) -> None:
    run = FakeRun({("git",): CommandResult(0, "80\t20\tsrc/a.py")})
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    task = await svc.apply_operation(task_id, "advance")

    assert task.state == "REVIEW"
    previous = task.history[-2]  # the ITERATING entry the gate wrote onto
    resolved = next(r for r in previous.responsibilities if r.key == RESPONSIBILITY_KEY)
    assert resolved.status is Status.MET
    assert "verified by tarot" in (resolved.comment or "")
    assert [c[1:3] for c in run.tarot_calls] == [("strands", "check"), ("tour", "check")]


async def test_the_checks_run_against_the_clone_and_the_repos_base(tmp_path) -> None:
    run = FakeRun({("git", "-C", CLONE, "diff"): CommandResult(0, "80\t20\tsrc/a.py")})
    svc = await make_service(tmp_path, gate=make_gate(run))
    await svc.apply_operation(await iterating_task(svc), "advance")

    assert run.tarot_calls[0] == (
        "tarot",
        "strands",
        "check",
        "--directory",
        CLONE,
        "--base",
        "origin/main",
    )


# -- refusal ----------------------------------------------------------------------


VIOLATION = "src/a.py:f: not claimed by any strand"


async def test_failing_checks_refuse_the_advance_and_leave_the_task_in_place(tmp_path) -> None:
    run = FakeRun(
        {
            ("git",): CommandResult(0, "80\t20\tsrc/a.py"),
            ("tarot", "strands"): CommandResult(1, VIOLATION),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    with pytest.raises(TarotGateRefused) as excinfo:
        await svc.apply_operation(task_id, "advance")

    assert VIOLATION in str(excinfo.value)
    assert "tarot_strand_seed" in str(excinfo.value)  # points at the authoring tools
    task = await svc.get_task(task_id)
    assert task.state == "ITERATING"  # the transition did not happen
    assert task.history[-1].to_state == "ITERATING"  # …and no history entry was appended


async def test_a_refusal_records_the_violations_as_the_responsibility_comment(tmp_path) -> None:
    run = FakeRun(
        {
            ("git",): CommandResult(0, "80\t20\tsrc/a.py"),
            ("tarot", "tour"): CommandResult(1, "pr-walkthrough: step 0 does not resolve"),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    with pytest.raises(TarotGateRefused):
        await svc.apply_operation(task_id, "advance")

    recorded = await responsibility(svc, task_id)
    assert recorded is not None
    assert recorded.status is Status.FAILED
    assert "step 0 does not resolve" in (recorded.comment or "")


async def test_a_second_attempt_is_still_refused(tmp_path) -> None:
    """The refusal is the enforcement, not the comment.

    A FAILED-with-comment responsibility counts as *resolved* by the workflow's own gate, so if
    the tarot gate only ran while the promise was pending, the retry would sail straight through.
    """
    run = FakeRun(
        {
            ("git",): CommandResult(0, "80\t20\tsrc/a.py"),
            ("tarot", "strands"): CommandResult(1, VIOLATION),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    for _ in range(2):
        with pytest.raises(TarotGateRefused):
            await svc.apply_operation(task_id, "advance")

    assert (await svc.get_task(task_id)).state == "ITERATING"


async def test_a_refusal_with_no_output_still_says_something(tmp_path) -> None:
    run = FakeRun(
        {
            ("git",): CommandResult(0, "80\t20\tsrc/a.py"),
            ("tarot", "strands"): CommandResult(1, "   "),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run))
    with pytest.raises(TarotGateRefused, match="failed"):
        await svc.apply_operation(await iterating_task(svc), "advance")


# -- honest refusals for a misconfigured host -------------------------------------


async def test_missing_binary_refuses_with_an_operator_facing_message(tmp_path) -> None:
    run = FakeRun()
    svc = await make_service(tmp_path, gate=make_gate(run, binary=None))
    task_id = await iterating_task(svc)

    with pytest.raises(TarotGateRefused) as excinfo:
        await svc.apply_operation(task_id, "advance")

    message = str(excinfo.value)
    assert "no `tarot` was found" in message
    assert "PANOPTICON_TAROT_BIN" in message
    assert "capabilities.tarot_review" in message  # how to opt back out
    assert run.tarot_calls == []
    assert (await svc.get_task(task_id)).state == "ITERATING"


async def test_a_clone_that_isnt_here_refuses_and_names_the_path(tmp_path) -> None:
    run = FakeRun()
    svc = await make_service(tmp_path, gate=make_gate(run, clone_exists=False))
    task_id = await iterating_task(svc)

    with pytest.raises(TarotGateRefused, match=CLONE):
        await svc.apply_operation(task_id, "advance")


async def test_an_unprovisioned_task_refuses_rather_than_passing(tmp_path) -> None:
    run = FakeRun()
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc, clone=None)

    with pytest.raises(TarotGateRefused, match="no clone readable"):
        await svc.apply_operation(task_id, "advance")


async def test_a_remote_runners_clone_refuses_and_names_the_host(tmp_path) -> None:
    """The clone exists — just not on *this* host. The dashboard's `v` degrades the same way."""
    run = FakeRun()
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)
    await svc.claim(task_id, "runner-b")
    await svc.register_runner("runner-b", host="build-box")

    with pytest.raises(TarotGateRefused, match="build-box"):
        await svc.apply_operation(task_id, "advance")


# -- the trivial-diff threshold ---------------------------------------------------


async def test_a_trivial_diff_skips_the_checks_and_auto_resolves(tmp_path) -> None:
    run = FakeRun({("git",): CommandResult(0, "3\t1\tsrc/a.py")})
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    task = await svc.apply_operation(task_id, "advance")

    assert task.state == "REVIEW"
    assert run.tarot_calls == []  # no tour to write for a four-line fix
    resolved = next(r for r in task.history[-2].responsibilities if r.key == RESPONSIBILITY_KEY)
    assert resolved.status is Status.MET
    assert "trivial diff (4 changed lines)" in (resolved.comment or "")


async def test_the_threshold_is_per_repo_overridable(tmp_path) -> None:
    run = FakeRun({("git",): CommandResult(0, "3\t1\tsrc/a.py")})
    svc = await make_service(
        tmp_path, gate=make_gate(run), capabilities={"tarot_review_threshold": 2}
    )
    await svc.apply_operation(await iterating_task(svc), "advance")
    assert run.tarot_calls != []  # 4 changed lines is no longer trivial


async def test_a_stray_boolean_threshold_falls_back_to_the_default(tmp_path) -> None:
    """`bool` is an `int` subclass — a stray `true` must not mean "threshold of 1"."""
    repo = Repo(id="r", name="n", git_url="u", capabilities={"tarot_review_threshold": True})
    assert TarotGate.threshold(repo) == DEFAULT_TRIVIAL_THRESHOLD


async def test_binary_counts_are_skipped_not_counted(tmp_path) -> None:
    run = FakeRun({("git",): CommandResult(0, "-\t-\timage.png\n2\t1\tsrc/a.py")})
    svc = await make_service(tmp_path, gate=make_gate(run))
    await svc.apply_operation(await iterating_task(svc), "advance")
    assert run.tarot_calls == []  # 3 real changed lines → trivial


async def test_an_unknowable_diff_runs_the_checks_rather_than_skipping(tmp_path) -> None:
    """A failed numstat (e.g. the base ref isn't fetched) is *unknown*, not *trivial*.

    The in-container gate this replaced summed the empty output to 0 and auto-resolved MET — the
    exact silent pass this gate exists to prevent.
    """
    run = FakeRun({("git", "-C", CLONE, "diff"): CommandResult(128, "fatal: bad revision")})
    svc = await make_service(tmp_path, gate=make_gate(run))
    await svc.apply_operation(await iterating_task(svc), "advance")
    assert [c[1:3] for c in run.tarot_calls] == [("strands", "check"), ("tour", "check")]


async def test_a_clone_that_pins_its_own_base_is_diffed_against_it(tmp_path) -> None:
    """A `tarot.base` clone must not be diffed against HEAD (which reads as a 0-line diff and
    would wave every such task through as trivial) nor against the repo default."""
    run = FakeRun(
        {
            ("git", "-C", CLONE, "config"): CommandResult(0, "pinned-ref\n"),
            ("git", "-C", CLONE, "diff"): CommandResult(0, "80\t20\tsrc/a.py"),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run))
    await svc.apply_operation(await iterating_task(svc), "advance")

    diff = next(c for c in run.calls if c[:2] == ("git", "-C") and "diff" in c)
    assert diff[-1] == "pinned-ref...HEAD"
    assert run.tarot_calls[0][-1] == CLONE  # no --base: tarot reads its own config


# -- when the gate must not run ---------------------------------------------------


async def test_a_repo_that_hasnt_opted_in_is_never_gated(tmp_path) -> None:
    run = FakeRun()
    svc = await make_service(tmp_path, gate=make_gate(run), opted_in=False)
    task_id = await iterating_task(svc)

    task = await svc.apply_operation(task_id, "advance")

    assert task.state == "REVIEW"
    assert run.calls == []  # not one command — not even the numstat


async def test_drop_is_never_gated(tmp_path) -> None:
    run = FakeRun({("tarot", "strands"): CommandResult(1, VIOLATION)})
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    task = await svc.apply_operation(task_id, "drop")

    assert task.state == "DROPPED"
    assert run.tarot_calls == []


async def test_a_free_move_is_never_gated(tmp_path) -> None:
    """`set_state` deliberately bypasses the declared graph *and* the gates — the user's override."""
    run = FakeRun({("tarot", "strands"): CommandResult(1, VIOLATION)})
    svc = await make_service(tmp_path, gate=make_gate(run))
    task_id = await iterating_task(svc)

    task = await svc.set_state(task_id, "REVIEW")

    assert task.state == "REVIEW"
    assert run.tarot_calls == []


async def test_advancing_out_of_another_state_is_never_gated(tmp_path) -> None:
    run = FakeRun({("tarot", "strands"): CommandResult(1, VIOLATION)})
    svc = await make_service(tmp_path, gate=make_gate(run))
    task = await svc.create_task("r1", "github-peer-reviewed")
    for key in ("plan-written", "token-estimated"):
        await svc.resolve_responsibility(task.id, key, status=Status.MET, comment="done")

    advanced = await svc.apply_operation(task.id, "advance")  # PLANNING → ITERATING

    assert advanced.state == "ITERATING"
    assert run.tarot_calls == []


async def test_a_repo_opted_in_mid_flight_gates_the_next_task_not_this_one(tmp_path) -> None:
    """Flipping the capability mid-flight doesn't retroactively gate a task already in ITERATING.

    The workflow declares the responsibility per (state, repo), so once the capability is on, the
    *declaration* is on too — but the promise was never seeded on this task's history entry, so
    there'd be nothing to resolve and no comment to write. The gate keys off the declaration, so
    it does engage; what this pins is that it doesn't crash on the absent promise.
    """
    run = FakeRun(
        {
            ("git",): CommandResult(0, "80\t20\tsrc/a.py"),
            ("tarot", "strands"): CommandResult(1, VIOLATION),
        }
    )
    svc = await make_service(tmp_path, gate=make_gate(run), opted_in=False)
    task_id = await iterating_task(svc)
    await svc.update_repo("r1", {"capabilities": {"tarot_review": True}})

    with pytest.raises(TarotGateRefused, match="not claimed"):
        await svc.apply_operation(task_id, "advance")

    assert await responsibility(svc, task_id) is None  # nothing promised, nothing recorded


async def test_a_workflow_that_declares_no_review_responsibility_is_never_gated(tmp_path) -> None:
    """`Spike` has an ITERATING state but promises no review artifacts.

    Gating on the state *label* would refuse every spike advance on an opted-in repo — with no
    responsibility to satisfy and no way out but a free move. The gate keys off the workflow's
    declaration instead, so a workflow that promises nothing is never gated.
    """
    run = FakeRun({("tarot", "strands"): CommandResult(1, VIOLATION)})
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 10_000))
    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
        id_factory=lambda: next(ids),
        tarot_gate=make_gate(run),
    )
    await svc.init()
    await svc.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            enabled_workflows=["spike"],
            capabilities={"tarot_review": True},
        )
    )
    task = await svc.create_task("r1", "spike")
    await svc.set_slug(task.id, "a-spike")
    await svc.record_provisioning(task.id, branch="panopticon/a-spike", clone=CLONE)

    advanced = await svc.apply_operation(task.id, "advance")

    assert advanced.state != "ITERATING"
    assert run.calls == []


# -- the real exit-code contract --------------------------------------------------


def write_stub_tarot(tmp_path: Path, *, exit_code: int, output: str) -> str:
    """A stub `tarot` honouring an exit code, exercised through the real subprocess runner."""
    script = tmp_path / "tarot"
    script.write_text(f'#!/bin/sh\nprintf %s "{output}"\nexit {exit_code}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


@pytest.mark.skipif(os.name != "posix", reason="the stub tarot is a /bin/sh script")
async def test_a_real_failing_binary_refuses_with_its_real_output(tmp_path) -> None:
    stub = write_stub_tarot(tmp_path, exit_code=1, output=VIOLATION)
    gate = TarotGate(cli=TarotCLI(tarot_binary=stub), clone_exists=lambda path: True)
    svc = await make_service(tmp_path, gate=gate)
    task_id = await iterating_task(svc, clone=str(tmp_path))

    with pytest.raises(TarotGateRefused, match="not claimed by any strand"):
        await svc.apply_operation(task_id, "advance")


@pytest.mark.skipif(os.name != "posix", reason="the stub tarot is a /bin/sh script")
async def test_a_real_passing_binary_allows_the_advance(tmp_path) -> None:
    stub = write_stub_tarot(tmp_path, exit_code=0, output="tarot: valid")
    gate = TarotGate(cli=TarotCLI(tarot_binary=stub), clone_exists=lambda path: True)
    svc = await make_service(tmp_path, gate=gate)
    # tmp_path isn't a git repo, so the numstat fails → "unknown, not trivial" → the checks run.
    task_id = await iterating_task(svc, clone=str(tmp_path))

    assert (await svc.apply_operation(task_id, "advance")).state == "REVIEW"
