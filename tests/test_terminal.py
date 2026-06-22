"""The terminal CLI (`panopticon`). The shared REST client it uses is covered in
test_client.py; the dashboard in test_dashboard.py."""

from __future__ import annotations

from typing import Any

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


class _RepoClient:
    def __init__(self, repo: dict[str, object]) -> None:
        self._repo = repo

    def get_repo(self, repo_id: str) -> dict[str, object]:
        return self._repo


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def login(self, creds_volume: str, command: list[str]) -> None:
        self.calls.append((creds_volume, command))


def test_cli_login_runs_against_repo_creds_volume() -> None:
    runner = _FakeRunner()
    rc = cli.main(
        ["login", "r1", "claude", "login"],
        client=_RepoClient({"id": "r1", "creds_volume": "creds-r1"}),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )
    assert rc == 0
    assert runner.calls == [("creds-r1", ["claude", "login"])]


def test_cli_login_defaults_to_claude() -> None:
    # `make login REPO=<id>` passes no command → claude (so it drops straight into the login flow).
    runner = _FakeRunner()
    rc = cli.main(
        ["login", "r1"],
        client=_RepoClient({"id": "r1", "creds_volume": "creds-r1"}),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )
    assert rc == 0
    assert runner.calls == [("creds-r1", ["claude"])]


def test_cli_login_errors_without_creds_volume() -> None:
    runner = _FakeRunner()
    rc = cli.main(
        ["login", "r1"],
        client=_RepoClient({"id": "r1"}),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )
    assert rc == 1 and runner.calls == []


def test_dashboard_under_supervisor_wires_the_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    # With --switch-file (set by the supervisor, ADR 0009 §6) the dashboard is wired with the
    # `t` (on_switch) and `s` (on_service) hooks; the dashboard itself stays running.
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "run",
        lambda _c, *, on_switch=None, on_service=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service),
    )
    cli.main(["dashboard", "--switch-file", "/tmp/x"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is not None and seen["on_service"] is not None


def test_standalone_dashboard_has_no_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "run",
        lambda _c, *, on_switch=None, on_service=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service),
    )
    cli.main(["dashboard"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is None and seen["on_service"] is None
