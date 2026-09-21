"""Acceptance: a merge made in a task's clone actually reaches the operator's repo, with **real
git** (skipped when git is absent). No fakes for git, no LLM:

  local origin → per-task clone → agent's branch + commit → merge into the base branch →
  request_push → one `Publisher` pass → the commits are in the operator's own repo.

This is the bug this feature exists for: before it, the merge lived only in the throwaway per-task
clone under the tasks dir and the operator's repo never saw it. Both halves of the checked-out-branch
story are covered here, because they're the difference between "landed" and "safe but not landed":

* a default non-bare repo **refuses** the base-branch push (``receive.denyCurrentBranch``) → the
  task branch still lands, so nothing is stranded (``partial``);
* with ``updateInstead`` configured — what quickstart and setup-repo arrange — the base branch
  lands too, and the operator's working tree updates with it (``pushed``).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import PushStatus, Repo
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.publisher import Publisher
from panopticon.sessionservice.spawn import prepare_workspace
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import LocalGitSelfReviewed

WORKFLOW = "local-git-self-reviewed"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_origin(path: Path, *, base: str) -> None:
    """A real non-bare repo on ``base`` — what a local ``git_url`` actually points at."""
    path.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch", base, str(path)], check=True, capture_output=True
    )
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("hi\n")
    _git(path, "add", "--all")
    _git(path, "commit", "--message", "init")


def _service(tmp_path: Path, origin: Path) -> TaskService:
    service = TaskService(
        SqlAlchemyStore(), {WORKFLOW: LocalGitSelfReviewed()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="acme/widgets",
                git_url=str(origin),  # a filesystem path — unreachable from inside a container
                enabled_workflows=[WORKFLOW],
            )
        )
    )
    return service


def _work_and_merge(clone: Path, *, base: str, branch: str) -> None:
    """What the agent does in its clone: branch, commit, then merge back into the base branch."""
    _git(clone, "config", "user.email", "agent@example.com")
    _git(clone, "config", "user.name", "agent")
    _git(clone, "checkout", "-b", branch)
    (clone / "WIDGET").write_text("green\n")
    _git(clone, "add", "--all")
    _git(clone, "commit", "--message", "paint the widget green")
    _git(clone, "checkout", base)
    _git(clone, "merge", "--no-ff", "--message", f"merge {branch}", branch)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize("base", ["main", "master"])
def test_merge_reaches_the_operators_repo(tmp_path: Path, base: str) -> None:
    """The whole point, on a repo whose base branch is `main` *or* `master` — the hardcoded
    `checkout main` this replaced silently did nothing useful on the latter."""
    origin = tmp_path / "origin"
    _make_origin(origin, base=base)
    # What quickstart sets automatically (and setup-repo offers): accept pushes to the checked-out
    # branch and update the files with them.
    _git(origin, "config", "receive.denyCurrentBranch", "updateInstead")

    service = _service(tmp_path, origin)
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", WORKFLOW)["id"]
        clones_root = tmp_path / "clones"
        clone = Path(
            prepare_workspace(
                task_id,
                client.get_repo("r1"),
                cache=CloneCache(str(tmp_path / "cache")),
                tasks_root=str(clones_root),
            )
        )
        # The base branch is discoverable from the clone alone — no assumption, no network.
        assert (
            _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") == f"origin/{base}"
        )

        branch = "panopticon/paint-it-green"
        client.set_slug(task_id, "paint-it-green")
        client.record_provisioning(task_id, branch, str(clone))
        _work_and_merge(clone, base=base, branch=branch)

        # Before the push, the merge exists *only* in the throwaway clone — the original bug.
        assert not (origin / "WIDGET").exists()

        client.request_push(task_id, base)
        publisher = Publisher(client, clones_root=str(clones_root))
        assert publisher.publish(client.get_task(task_id)) is PushStatus.PUSHED

        # It landed: the operator's repo has the commits *and* the updated working tree.
        assert (origin / "WIDGET").read_text() == "green\n"
        assert "paint the widget green" in _git(origin, "log", "--oneline", base)
        assert branch in _git(origin, "branch", "--list", branch)  # the backup went too
        assert client.get_task(task_id)["push"]["status"] == "pushed"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_refused_base_branch_still_leaves_the_work_in_the_operators_repo(tmp_path: Path) -> None:
    """The unconfigured case. Git refuses the base-branch push, but the task branch is never the
    checked-out one — so the commits reach the operator's repo regardless and can be merged by
    hand. Nothing is ever stranded in a clone that cleanup will delete."""
    origin = tmp_path / "origin"
    _make_origin(origin, base="main")  # deliberately *not* configured with updateInstead

    service = _service(tmp_path, origin)
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", WORKFLOW)["id"]
        clones_root = tmp_path / "clones"
        clone = Path(
            prepare_workspace(
                task_id,
                client.get_repo("r1"),
                cache=CloneCache(str(tmp_path / "cache")),
                tasks_root=str(clones_root),
            )
        )
        branch = "panopticon/paint-it-green"
        client.set_slug(task_id, "paint-it-green")
        client.record_provisioning(task_id, branch, str(clone))
        _work_and_merge(clone, base="main", branch=branch)

        client.request_push(task_id, "main")
        publisher = Publisher(client, clones_root=str(clones_root))
        assert publisher.publish(client.get_task(task_id)) is PushStatus.PARTIAL

        # The base branch is untouched (git refused it) …
        assert "paint the widget green" not in _git(origin, "log", "--oneline", "main")
        # … but the work is *there*, on the task branch, ready to merge by hand.
        assert branch in _git(origin, "branch", "--list", branch)
        assert "paint the widget green" in _git(origin, "log", "--oneline", branch)

        detail = client.get_task(task_id)["push"]["detail"]
        assert "receive.denyCurrentBranch updateInstead" in detail  # the fix, spelled out
        assert f"git merge {branch}" in detail  # and the way through without it
