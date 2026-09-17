"""Host-side publishing: the session service pushes a task's merge back to origin and records
the outcome. Unit tests pin the gates, the two-ref push order, and every failure classification;
an integration test drives the real task service over REST. No Docker, no LLM — `git` is a fake
command-runner."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones
from panopticon.core.models import PushStatus, Repo
from panopticon.sessionservice.publisher import Publisher, explain_push_failure
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import LocalGitSelfReviewed

#: What git says when it refuses a push to a non-bare repo's checked-out branch.
DENY_CURRENT_BRANCH = (
    "remote: error: refusing to update checked out branch: refs/heads/main\n"
    "remote: error: By default, updating the current branch in a non-bare repository\n"
    "! [remote rejected] main -> main (branch is currently checked out)"
)
NON_FAST_FORWARD = (
    "! [rejected] main -> main (non-fast-forward)\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
    "hint: its remote counterpart. Integrate the remote changes (fetch first)."
)


def _recording_runner(
    *, reject: dict[str, str] | None = None
) -> tuple[list[list[str]], Callable[..., str]]:
    """A fake git runner capturing each argv; ``reject`` maps a branch name to the stderr git
    should fail that push with (so a test can refuse the base branch but accept the backup)."""
    calls: list[list[str]] = []
    rejections = reject or {}

    def run(args: Sequence[str], *, check: bool = True) -> str:
        argv = list(args)
        calls.append(argv)
        if argv[3:4] == ["push"] and argv[-1] in rejections:
            raise subprocess.CalledProcessError(1, argv, stderr=rejections[argv[-1]])
        return ""

    return calls, run


class _FakeClient:
    """A task-service client stub: answers the execution lookup, captures record_push calls."""

    def __init__(self, *, runner_type: str = "docker") -> None:
        self._runner_type = runner_type
        self.recorded: list[tuple[str, str, str | None]] = []

    def workflow_execution(self, name: str) -> JsonObj:
        return {
            "runner_type": self._runner_type,
            "script": "",
            "clone_repo": False,
            "workdir": None,
        }

    def record_push(self, task_id: str, status: str, detail: str | None = None) -> JsonObj:
        self.recorded.append((task_id, status, detail))
        return {"id": task_id}


def _publisher(client: object, run: Callable[..., str]) -> Publisher:
    return Publisher(client, clones_root="/clones", git=GitClones(run=run))  # type: ignore[arg-type]


def _task(**overrides: object) -> JsonObj:
    task: JsonObj = {
        "id": "t1",
        "workflow": "local-git-self-reviewed",
        "branch": "panopticon/fix-widget",
        "clone": "/clones/t1",
        "push": {"branch": "main", "status": "requested", "detail": None},
    }
    task.update(overrides)
    return task


# -- the gates ----------------------------------------------------------------------


def test_skips_a_task_that_never_requested_a_push() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()
    assert _publisher(client, run).publish(_task(push=None)) is None
    assert calls == []
    assert client.recorded == []


def test_skips_a_push_that_is_already_resolved() -> None:
    # Recording the outcome is what clears the gate, so a second pass over the same snapshot
    # must not push again (the push itself is not idempotent).
    calls, run = _recording_runner()
    client = _FakeClient()
    resolved = _task(push={"branch": "main", "status": "pushed", "detail": "pushed main"})
    assert _publisher(client, run).publish(resolved) is None
    assert calls == []
    assert client.recorded == []


def test_skips_a_shell_workflow_task_which_has_no_clone() -> None:
    calls, run = _recording_runner()
    client = _FakeClient(runner_type="shell")
    assert _publisher(client, run).publish(_task(workflow="setup-repo")) is None
    assert calls == []
    assert client.recorded == []


# -- the happy path -----------------------------------------------------------------


def test_pushes_the_backup_branch_before_the_base_branch() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()

    assert _publisher(client, run).publish(_task()) is PushStatus.PUSHED

    # Order matters: the task branch is never the destination's checked-out branch, so it lands
    # even when the base branch is refused — pushing it first is what keeps the work reachable.
    assert calls == [
        ["git", "-C", "/clones/t1", "push", "origin", "panopticon/fix-widget"],
        ["git", "-C", "/clones/t1", "push", "origin", "main"],
    ]
    assert client.recorded == [
        ("t1", "pushed", "pushed main to origin (and panopticon/fix-widget)")
    ]


def test_pushes_only_the_base_branch_when_the_task_has_no_branch_recorded() -> None:
    calls, run = _recording_runner()
    client = _FakeClient()

    assert _publisher(client, run).publish(_task(branch=None)) is PushStatus.PUSHED
    assert calls == [["git", "-C", "/clones/t1", "push", "origin", "main"]]


def test_falls_back_to_the_clones_root_when_the_task_records_no_clone_path() -> None:
    calls, run = _recording_runner()
    assert _publisher(_FakeClient(), run).publish(_task(clone=None)) is PushStatus.PUSHED
    assert all(call[2] == "/clones/t1" for call in calls)


# -- partial: the backup landed, the base branch didn't -----------------------------


def test_a_refused_base_branch_is_partial_not_failed() -> None:
    calls, run = _recording_runner(reject={"main": DENY_CURRENT_BRANCH})
    client = _FakeClient()

    assert _publisher(client, run).publish(_task()) is PushStatus.PARTIAL

    assert len(calls) == 2  # the backup was attempted and succeeded before the base branch
    (task_id, status, detail) = client.recorded[0]
    assert (task_id, status) == ("t1", "partial")
    assert detail is not None
    assert "receive.denyCurrentBranch updateInstead" in detail  # the remedy, spelled out
    assert "git merge panopticon/fix-widget" in detail  # and the fallback that needs no config


def test_both_pushes_failing_is_failed_and_reports_both() -> None:
    calls, run = _recording_runner(
        reject={"main": DENY_CURRENT_BRANCH, "panopticon/fix-widget": "fatal: unreachable"}
    )
    client = _FakeClient()

    assert _publisher(client, run).publish(_task()) is PushStatus.FAILED

    assert len(calls) == 2  # a failed backup doesn't stop us trying the base branch
    (_, _, detail) = client.recorded[0]
    assert detail is not None
    assert "unreachable" in detail  # the backup's own failure is surfaced too
    assert "git merge" not in detail  # …and the fallback isn't offered, since nothing landed


# -- failure classification ---------------------------------------------------------


def test_explains_a_checked_out_branch_refusal() -> None:
    detail = explain_push_failure(DENY_CURRENT_BRANCH, branch="main", task_branch="panopticon/x")
    assert "checked out" in detail
    assert "git config receive.denyCurrentBranch updateInstead" in detail


def test_explains_a_non_fast_forward() -> None:
    detail = explain_push_failure(NON_FAST_FORWARD, branch="main", task_branch="panopticon/x")
    assert "fast-forward" in detail
    assert "Fetch origin" in detail


def test_explains_a_dirty_worktree_at_the_other_end() -> None:
    stderr = "remote: error: Working directory has unstaged changes"
    detail = explain_push_failure(stderr, branch="main", task_branch=None)
    assert "uncommitted changes" in detail
    assert "Commit or stash" in detail


def test_explains_an_unreachable_or_unauthenticated_origin() -> None:
    stderr = "fatal: '/home/me/src/thing' does not appear to be a git repository"
    detail = explain_push_failure(stderr, branch="main", task_branch=None)
    assert "no credentials" in detail or "could not reach" in detail


def test_unrecognized_failures_carry_gits_own_words() -> None:
    detail = explain_push_failure("something odd happened", branch="main", task_branch=None)
    assert "something odd happened" in detail


def test_a_long_stderr_is_truncated_rather_than_pasted_whole() -> None:
    detail = explain_push_failure("x" * 5_000, branch="main", task_branch=None)
    assert len(detail) < 600
    assert detail.endswith("…")


# -- integration: the real task service over REST ------------------------------------


def test_publisher_against_the_real_service(tmp_path: Path) -> None:
    """End to end over REST: the agent requests, the publisher pushes and records, and a second
    pass is a no-op (the host loop calls this on every task each pass)."""
    service = TaskService(
        SqlAlchemyStore(),
        {"local-git-self-reviewed": LocalGitSelfReviewed()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="acme/widgets",
                git_url="/home/me/widgets",  # a local path: exactly the case the container can't push to
                enabled_workflows=["local-git-self-reviewed"],  # the workflow is opt-in
            )
        )
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "local-git-self-reviewed")["id"]
        client.set_slug(task_id, "fix-widget")
        client.record_provisioning(task_id, "panopticon/fix-widget", f"/clones/{task_id}")
        task = client.request_push(task_id, "main")
        assert task["push"]["status"] == "requested"

        calls, run = _recording_runner()
        publisher = Publisher(client, clones_root="/clones", git=GitClones(run=run))

        assert publisher.publish(client.get_task(task_id)) is PushStatus.PUSHED
        assert client.get_task(task_id)["push"]["status"] == "pushed"
        assert len(calls) == 2

        assert publisher.publish(client.get_task(task_id)) is None  # resolved → no second push
        assert len(calls) == 2
