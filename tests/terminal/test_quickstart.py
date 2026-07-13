"""Unit tests for panopticon.terminal.quickstart."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from panopticon.terminal import quickstart as qs


def test_detect_git_url_from_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        r = MagicMock()
        r.stdout = "https://github.com/example/repo.git\n"
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert qs.detect_git_url() == "https://github.com/example/repo.git"


def test_detect_git_url_fallback_on_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert qs.detect_git_url() == qs._FALLBACK_GIT_URL


def test_detect_git_url_fallback_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert qs.detect_git_url() == qs._FALLBACK_GIT_URL


def test_ensure_secrets_file_creates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import panopticon.core.dirs as dirs_mod

    monkeypatch.setattr(dirs_mod, "user_config_dir", lambda: tmp_path)

    # Returns the file's name (relative to the secrets dir), written under <config>/secrets/.
    name = qs.ensure_secrets_file()
    assert name == "panopticon.env"
    secrets = tmp_path / "secrets" / name
    assert secrets.exists()
    content = secrets.read_text()
    # Placeholder assignments are commented out — the user uncomments the one they use.
    assert "# CLAUDE_CODE_OAUTH_TOKEN=" in content
    assert "# GH_TOKEN=" in content
    assert "\nCLAUDE_CODE_OAUTH_TOKEN=" not in content
    assert "\nGH_TOKEN=" not in content


def test_ensure_secrets_file_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import panopticon.core.dirs as dirs_mod

    monkeypatch.setattr(dirs_mod, "user_config_dir", lambda: tmp_path)

    existing_content = "MY_EXISTING_SECRET=abc\n"
    secrets_path = tmp_path / "secrets" / "panopticon.env"
    secrets_path.parent.mkdir(parents=True)
    secrets_path.write_text(existing_content)

    qs.ensure_secrets_file()
    assert secrets_path.read_text() == existing_content


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/Unsupervisedcom/panopticon.git", "panopticon"),
        ("https://github.com/example/repo", "repo"),
        ("git@github.com:acme/Widget.git", "widget"),
        ("https://github.com/acme/thing.git/", "thing"),
    ],
)
def test_repo_id_from_url(url: str, expected: str) -> None:
    assert qs.repo_id_from_url(url) == expected


def test_setup_repo_dedups_on_remote_url(capsys: pytest.CaptureFixture[str]) -> None:
    # A registered repo whose git_url matches (modulo a trailing ``.git``) → no re-registration;
    # its (id, name) is returned.
    class _HasRepo:
        create_repo_called = False

        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "other", "name": "acme/other", "git_url": "https://github.com/x/y"}]

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

    fake_client = _HasRepo()
    repo_id, name = qs.setup_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("other", "acme/other")
    assert not fake_client.create_repo_called
    assert "already configured" in capsys.readouterr().out


def test_setup_repo_creates_when_absent() -> None:
    created: dict[str, Any] = {}

    class _Empty:
        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "unrelated", "git_url": "https://github.com/a/b.git"}]

        def create_repo(
            self, repo_id: str, name: str, git_url: str, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, name=name, git_url=git_url, **kw)
            return {}

    repo_id, name = qs.setup_repo(_Empty(), "https://github.com/x/y.git", "panopticon.env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("y", "y")
    assert created["repo_id"] == "y"
    assert created["name"] == "y"
    assert created["git_url"] == "https://github.com/x/y.git"
    assert created["env_file"] == "panopticon.env"
    # setup-repo is opt-out (enabled everywhere) so quickstart no longer enables it explicitly.
    assert "enabled_workflows" not in created


def test_setup_repo_dedups_on_derived_id_when_remote_differs() -> None:
    # The existing repo's stored remote (ssh form) doesn't normalize-match the https origin, but its
    # id equals the id we'd derive — so it's reused (not re-created, which would collide).
    class _HasRepo:
        create_repo_called = False

        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "y", "name": "x/y", "git_url": "git@github.com:x/y.git"}]

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

    fake_client = _HasRepo()
    repo_id, name = qs.setup_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("y", "x/y")
    assert not fake_client.create_repo_called


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/repos")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_setup_repo_recovers_from_create_conflict() -> None:
    # A create that collides (409) — e.g. the repo exists but dedup missed its remote — falls back
    # to reusing the existing repo rather than crashing quickstart.
    class _Conflict:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            raise _http_status_error(409)

    repo_id, name = qs.setup_repo(_Conflict(), "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("y", "y")


def test_setup_repo_reraises_non_conflict_create_error() -> None:
    class _Boom:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            raise _http_status_error(500)

    with pytest.raises(httpx.HTTPStatusError):
        qs.setup_repo(_Boom(), "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]


def test_ensure_setup_repo_task_creates_when_none() -> None:
    created: dict[str, Any] = {}

    class _NoTasks:
        def list_tasks(self) -> list[dict[str, object]]:
            return [
                # Other repo / other workflow / terminal — none reusable.
                {"id": "t1", "repo_id": "other", "workflow": "setup-repo", "state": "RUNNING"},
                {"id": "t2", "repo_id": "y", "workflow": "spike", "state": "PLANNING"},
                {"id": "t3", "repo_id": "y", "workflow": "setup-repo", "state": "COMPLETE"},
                {"id": "t4", "repo_id": "y", "workflow": "setup-repo", "state": "DROPPED"},
            ]

        def create_task(
            self, repo_id: str, workflow: str, memo: str | None = None, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, workflow=workflow, memo=memo)
            return {"id": "new-task"}

    task_id = qs.ensure_setup_repo_task(_NoTasks(), "y", "acme/y")  # type: ignore[arg-type]
    assert task_id == "new-task"
    # Created via the shared helper, seeded with its memo.
    assert created == {"repo_id": "y", "workflow": "setup-repo", "memo": "Set up the acme/y repo."}


def test_ensure_setup_repo_task_reuses_non_terminal() -> None:
    class _HasTask:
        create_task_called = False

        def list_tasks(self) -> list[dict[str, object]]:
            return [
                {"id": "t3", "repo_id": "y", "workflow": "setup-repo", "state": "COMPLETE"},
                {"id": "t5", "repo_id": "y", "workflow": "setup-repo", "state": "RUNNING"},
            ]

        def create_task(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_task_called = True
            return {"id": "new-task"}

    fake_client = _HasTask()
    task_id = qs.ensure_setup_repo_task(fake_client, "y", "acme/y")  # type: ignore[arg-type]
    assert task_id == "t5"
    assert not fake_client.create_task_called


def test_ensure_setup_repo_task_returns_none_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    # Best-effort: a task-service error (e.g. an older service without the workflow) → None, so
    # quickstart still opens the console rather than crashing.
    class _Broken:
        def list_tasks(self) -> list[dict[str, object]]:
            return []

        def create_task(self, *a: Any, **kw: Any) -> dict[str, object]:
            raise _http_status_error(400)

    assert qs.ensure_setup_repo_task(_Broken(), "y", "acme/y") is None  # type: ignore[arg-type]
    assert "Could not start a setup-repo task" in capsys.readouterr().out
