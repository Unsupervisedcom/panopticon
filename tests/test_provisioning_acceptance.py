"""Slice 7 acceptance (ADR 0010/0011): the host-side provisioning path end to end with **real git**
(skipped when git is absent). No fakes for git, no LLM:

  create task → spawn-prep clones the per-task checkout + points origin at the forge → agent sets
  slug → the daemon observes it and branches the clone (`panopticon/<slug>`) → the task service
  records the branch + clone path.

The agent (claude) and the container/docker mount are out of scope here (covered by the runner's
unit tests + the Slice 2 acceptance); this proves the git reality of provisioning.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.daemon import run_daemon
from panopticon.sessionservice.spawn import prepare_workspace
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_provisioning_end_to_end_with_real_git(tmp_path: Path) -> None:
    # A real "forge" repo with a base branch — stands in for both the cache source and origin.
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--initial-branch", "main", str(origin)], check=True, capture_output=True)
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "README").write_text("hi\n")
    _git(origin, "add", "--all")
    _git(origin, "commit", "--message", "init")

    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url=str(origin), default_base="main"))
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        task_id = client.create_task("r1", "spike")["id"]

        # Spawn-prep: the task's writable per-task clone (real `git clone --local` via the cache),
        # with origin pointed at the forge here (not deferred to provisioning).
        clones_root = tmp_path / "clones"
        cache = CloneCache(str(tmp_path / "cache"))
        per_task = Path(
            prepare_workspace(task_id, client.get_repo("r1"), cache=cache, tasks_root=str(clones_root))
        )
        assert (per_task / "README").read_text() == "hi\n"  # working copy on the base branch
        assert _git(per_task, "branch", "--show-current") == "main"
        assert _git(per_task, "remote", "get-url", "origin") == str(origin)  # origin → forge at spawn-prep

        # The agent sets its slug; the daemon observes it and provisions in one pass.
        client.set_slug(task_id, "fix-widget")
        passes = {"n": 0}

        def until() -> bool:
            done = passes["n"] >= 1
            passes["n"] += 1
            return done

        run_daemon(client, tasks_root=str(clones_root), until=until, sleep=lambda _s: None)

        # The per-task clone is now on the feature branch (origin already at the forge from spawn-prep).
        assert _git(per_task, "branch", "--show-current") == "panopticon/fix-widget"
        assert _git(per_task, "remote", "get-url", "origin") == str(origin)

        # The task service recorded the branch + clone path (a pure recorded fact).
        got = client.get_task(task_id)
        assert got["branch"] == "panopticon/fix-widget"
        assert got["clone"] == str(per_task)
