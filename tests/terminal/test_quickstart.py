"""Unit tests for panopticon.terminal.quickstart."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
    # A registered repo whose git_url matches (modulo a trailing ``.git``) → no re-registration.
    class _HasRepo:
        create_repo_called = False

        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "other", "git_url": "https://github.com/x/y"}]

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

    fake_client = _HasRepo()
    qs.setup_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
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

    qs.setup_repo(_Empty(), "https://github.com/x/y.git", "panopticon.env")  # type: ignore[arg-type]
    assert created["repo_id"] == "y"
    assert created["name"] == "y"
    assert created["git_url"] == "https://github.com/x/y.git"
    assert created["env_file"] == "panopticon.env"
