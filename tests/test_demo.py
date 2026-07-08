"""Acceptance tests for `panopticon demo` / run_demo().

Spins up a real in-process task service (no Docker, no LLM) and asserts that
run_demo() registers exactly one repo and creates exactly two spike tasks.
"""

from __future__ import annotations

import asyncio
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
from panopticon.terminal.demo import run_demo
from panopticon.workflows import Spike


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
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n")
    subprocess.run(["git", "-C", str(repo), "init", "--initial-branch=main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "--all"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit",
         "--message=init", "--author=test <t@t.invalid>"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.invalid",
             "HOME": str(tmp_path)},
    )
    return repo


def test_demo_registers_one_repo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    repos = demo_client.list_repos()
    assert len(repos) == 1
    assert repos[0]["git_url"] == str(sample_repo)


def test_demo_creates_two_tasks(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert len(tasks) == 2


def test_demo_tasks_use_spike_workflow(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["workflow"] == "spike" for t in tasks)


def test_demo_tasks_start_in_iterating_state(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["state"] == "ITERATING" for t in tasks)


def test_demo_tasks_belong_to_demo_repo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    repos = demo_client.list_repos()
    tasks = demo_client.list_tasks()
    repo_id = repos[0]["id"]
    assert all(t["repo_id"] == repo_id for t in tasks)


def test_demo_tasks_have_memo(
    demo_client: TaskServiceClient, sample_repo: Path
) -> None:
    run_demo("http://testserver", client=demo_client, repo_path=sample_repo)
    tasks = demo_client.list_tasks()
    assert all(t["memo"] for t in tasks)
