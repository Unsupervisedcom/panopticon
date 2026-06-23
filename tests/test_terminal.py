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
    def __init__(
        self,
        repo: dict[str, object],
        tasks: list[dict[str, object]] | None = None,
        registrations: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self._repo = repo
        self._tasks = tasks or []
        self._registrations = registrations or {}
        self.released: list[str] = []

    def get_repo(self, repo_id: str) -> dict[str, object]:
        return self._repo

    def list_tasks(self) -> list[dict[str, object]]:
        return self._tasks

    def list_registrations(self, task_id: str) -> list[dict[str, str]]:
        return self._registrations.get(task_id, [])

    def release(self, task_id: str) -> dict[str, object]:
        self.released.append(task_id)
        return {}


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.stopped: list[str] = []

    def login(self, creds_volume: str, command: list[str]) -> None:
        self.calls.append((creds_volume, command))

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


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
    # `panopticon login <repo>` passes no command → claude (so it drops straight into the login flow).
    runner = _FakeRunner()
    rc = cli.main(
        ["login", "r1"],
        client=_RepoClient({"id": "r1", "creds_volume": "creds-r1"}),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )
    assert rc == 0
    assert runner.calls == [("creds-r1", ["claude"])]


def test_cli_login_restarts_the_repos_running_containers() -> None:
    # After writing the creds, login restarts the repo's live task containers so they pick them up.
    runner = _FakeRunner()
    client = _RepoClient(
        {"id": "r1", "creds_volume": "creds-r1"},
        tasks=[
            {"id": "t1", "repo_id": "r1", "state": "ITERATING"},  # live → restarted
            {"id": "t2", "repo_id": "r2", "state": "ITERATING"},  # other repo → skipped
        ],
        registrations={"t1": [{"container_id": "panopticon-t1"}]},
    )
    rc = cli.main(["login", "r1"], client=client, runner=runner)  # type: ignore[arg-type]
    assert rc == 0
    assert runner.calls == [("creds-r1", ["claude"])]  # logged in first
    assert runner.stopped == ["panopticon-t1"] and client.released == ["t1"]  # then restarted


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
        lambda _c, *, on_switch=None, on_service=None, login=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service, login=login),
    )
    cli.main(["dashboard", "--switch-file", "/tmp/x"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is not None and seen["on_service"] is not None
    assert seen["login"] is not None  # the repos screen's `l` hook is wired


def test_standalone_dashboard_has_no_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard, "run",
        lambda _c, *, on_switch=None, on_service=None, login=None, artifacts_root=None: seen.update(on_switch=on_switch, on_service=on_service, login=login),
    )
    cli.main(["dashboard"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is None and seen["on_service"] is None
    assert seen["login"] is not None  # login works standalone (unlike the switch hooks)
