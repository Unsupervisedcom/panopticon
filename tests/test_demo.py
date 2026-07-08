"""Acceptance tests for `panopticon demo` / run_demo().

Spins up a real in-process task service (no Docker, no LLM) and asserts that
run_demo() registers exactly one repo and creates exactly two spike tasks.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal.demo import _SAMPLE_REPO_SRC, _init_sample_repo, run_demo
from panopticon.workflows import Spike

_needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


@pytest.fixture
def demo_client(tmp_path: Path) -> Iterator[TaskServiceClient]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
    )
    asyncio.run(service.init())
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A minimal git repo the demo can register without calling _init_sample_repo."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n")
    subprocess.run(["git", "-C", str(repo), "init", "--initial-branch=main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "--all"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.name=test",
         "-c", "user.email=t@t.invalid",
         "commit", "--message=init"],
        check=True, capture_output=True,
    )
    return repo


# -- _init_sample_repo (production path) -----------------------------------------

@_needs_git
def test_init_sample_repo_returns_a_git_repo(tmp_path: Path) -> None:
    repo = _init_sample_repo(src=_SAMPLE_REPO_SRC)
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip()  # at least one commit


@_needs_git
def test_init_sample_repo_copies_seed_files(tmp_path: Path) -> None:
    repo = _init_sample_repo(src=_SAMPLE_REPO_SRC)
    assert (repo / "README.md").exists()
    assert (repo / "hello.py").exists()


@_needs_git
def test_init_sample_repo_succeeds_without_global_git_identity(tmp_path: Path) -> None:
    """Must not fail on a fresh machine with no global user.name / user.email."""
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
    # Override HOME so git can't read ~/.gitconfig
    env["HOME"] = str(tmp_path)
    import unittest.mock
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        repo = _init_sample_repo(src=_SAMPLE_REPO_SRC)
    assert repo.exists()


# -- run_demo (REST acceptance) --------------------------------------------------

@_needs_git
def test_demo_registers_one_repo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    repos = demo_client.list_repos()
    assert len(repos) == 1
    assert repos[0]["git_url"] == str(sample_repo)


@_needs_git
def test_demo_creates_two_tasks(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert len(tasks) == 2


@_needs_git
def test_demo_tasks_use_spike_workflow(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["workflow"] == "spike" for t in tasks)


@_needs_git
def test_demo_tasks_start_in_iterating_state(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["state"] == "ITERATING" for t in tasks)


@_needs_git
def test_demo_tasks_belong_to_demo_repo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    repos = demo_client.list_repos()
    tasks = demo_client.list_tasks()
    repo_id = repos[0]["id"]
    assert all(t["repo_id"] == repo_id for t in tasks)


@_needs_git
def test_demo_tasks_have_memo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["memo"] for t in tasks)
