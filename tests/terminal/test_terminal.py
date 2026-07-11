"""The terminal CLI (`panopticon`). The shared REST client it uses is covered in
test_client.py; the dashboard in test_dashboard.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from panopticon.terminal import __main__ as cli


class _FakeClient:
    def list_tasks(self) -> list[dict[str, object]]:
        return [{"id": "t1", "state": "ITERATING", "turn": "agent", "slug": None}]


def test_cli_tasks_lists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["tasks"], client=_FakeClient())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out and "ITERATING" in out and "agent" in out


def test_dashboard_under_supervisor_wires_the_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    # With --switch-file (set by the supervisor, ADR 0009 §6) the dashboard is wired with the
    # `t` (on_switch), `s` (on_service), and `u` (on_runner) hooks; the dashboard stays running.
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service, on_runner=on_runner),
    )
    cli.main(["dashboard", "--switch-file", "/tmp/x"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is not None and seen["on_service"] is not None and seen["on_runner"] is not None


def test_standalone_dashboard_has_no_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service, on_runner=on_runner),
    )
    cli.main(["dashboard"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is None and seen["on_service"] is None and seen["on_runner"] is None


# ---------------------------------------------------------------------------
# quickstart helpers
# ---------------------------------------------------------------------------


def test_detect_git_url_from_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        r = MagicMock()
        r.stdout = "https://github.com/example/repo.git\n"
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._detect_git_url() == "https://github.com/example/repo.git"


def test_detect_git_url_fallback_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._detect_git_url() == cli._FALLBACK_GIT_URL


def test_detect_git_url_fallback_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._detect_git_url() == cli._FALLBACK_GIT_URL


def test_ensure_secrets_file_creates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import panopticon.core.dirs as dirs_mod
    monkeypatch.setattr(dirs_mod, "user_config_dir", lambda: tmp_path)

    path = cli._ensure_secrets_file()
    secrets = Path(path)
    assert secrets.exists()
    content = secrets.read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=" in content
    assert "GH_TOKEN=" in content


def test_ensure_secrets_file_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import panopticon.core.dirs as dirs_mod
    monkeypatch.setattr(dirs_mod, "user_config_dir", lambda: tmp_path)

    existing_content = "MY_EXISTING_SECRET=abc\n"
    secrets_path = tmp_path / "panopticon.env"
    secrets_path.write_text(existing_content)

    cli._ensure_secrets_file()
    assert secrets_path.read_text() == existing_content


def test_setup_panopticon_repo_already_exists(capsys: pytest.CaptureFixture[str]) -> None:
    class _AlreadyExists:
        create_repo_called = False

        def get_repo(self, repo_id: str) -> dict[str, object]:
            return {"id": repo_id}

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

    fake_client = _AlreadyExists()
    cli._setup_panopticon_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert not fake_client.create_repo_called
    assert "already configured" in capsys.readouterr().out


def test_setup_panopticon_repo_creates_on_404(capsys: pytest.CaptureFixture[str]) -> None:
    created: dict[str, Any] = {}

    class _NotFound:
        def get_repo(self, repo_id: str) -> dict[str, object]:
            resp = MagicMock()
            resp.status_code = 404
            raise httpx.HTTPStatusError("not found", request=MagicMock(), response=resp)

        def create_repo(self, repo_id: str, name: str, git_url: str, **kw: Any) -> dict[str, object]:
            created.update(repo_id=repo_id, name=name, git_url=git_url, **kw)
            return {}

    cli._setup_panopticon_repo(_NotFound(), "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert created["repo_id"] == "panopticon"
    assert created["git_url"] == "https://github.com/x/y.git"
    assert created["env_file"] == "/tmp/env"


def test_setup_panopticon_repo_reraises_non_404(capsys: pytest.CaptureFixture[str]) -> None:
    class _ServerError:
        def get_repo(self, repo_id: str) -> dict[str, object]:
            resp = MagicMock()
            resp.status_code = 500
            raise httpx.HTTPStatusError("server error", request=MagicMock(), response=resp)

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            return {}

    with pytest.raises(httpx.HTTPStatusError):
        cli._setup_panopticon_repo(_ServerError(), "https://x.git", "/tmp/env")  # type: ignore[arg-type]


def test_quickstart_invokes_all_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli, "_run_migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_start_sessions", lambda: calls.append("sessions"))
    monkeypatch.setattr(cli, "_wait_for_service", lambda url, **kw: calls.append("wait"))
    monkeypatch.setattr(cli, "_ensure_secrets_file", lambda: (calls.append("secrets"), "/tmp/env")[1])
    monkeypatch.setattr(cli, "_detect_git_url", lambda: (calls.append("git_url"), "https://x.git")[1])
    monkeypatch.setattr(cli, "_setup_panopticon_repo", lambda c, g, e: calls.append("setup"))

    from panopticon.terminal import console
    monkeypatch.setattr(console, "run_console_local", lambda url: calls.append("console"))

    rc = cli.main(["quickstart"])
    assert rc == 0
    assert calls == ["migrate", "sessions", "wait", "secrets", "git_url", "setup", "console"]
