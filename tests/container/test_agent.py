"""The CLI-agnostic agent launcher: the deterministic bootstrap (resolve the adapter, render the
workflow's skills + turn-flip hooks, wire MCP + trust) then launch. No LLM — the real CLI exec is a
fake here. The claude-specific seams live in :mod:`tests.container.test_claude`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.container import agent
from panopticon.container.cli.claude import ClaudeAgentCLI


class _FakeClient:
    def __init__(
        self,
        skills: list[dict[str, str]],
        operations: dict[str, str] | None = None,
        overview: str = "# the workflow",
    ) -> None:
        self._skills = skills
        self._operations = operations or {}
        self._overview = overview
        self.lifecycle_calls: list[dict[str, str | None]] = []

    def list_skills(self, task_id: str) -> list[dict[str, str]]:
        return self._skills

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def workflow_overview(self, task_id: str) -> str:
        return self._overview

    def report_lifecycle(
        self, task_id: str, runner_id: str, phase: str, detail: str | None = None
    ) -> dict[str, str | None]:
        self.lifecycle_calls.append(
            {"task_id": task_id, "runner_id": runner_id, "phase": phase, "detail": detail}
        )
        return {}


def test_main_bootstraps_into_a_container_local_config_dir_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-test")
    events: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(  # type: ignore[arg-type,return-value]
            [{"name": "s", "description": "d", "instructions": "i"}], {"advance": "COMPLETE"}
        ),
        home=tmp_path,
        launch=lambda cfg: events.append(f"launch:{cfg}"),
        on_exit=lambda: events.append("on_exit"),
    )
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "s.md").exists()  # skills rendered...
    assert (commands / "advance.md").exists()  # ...operations rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert (
        tmp_path / ".claude" / ClaudeAgentCLI.MCP_CONFIG_FILE
    ).exists()  # ...MCP server wired...
    assert (
        tmp_path / ".claude" / ClaudeAgentCLI.WORKFLOW_OVERVIEW_FILE
    ).exists()  # ...workflow map written...
    trust = json.loads((tmp_path / ".claude" / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert (
        trust["projects"][str(Path.cwd())]["hasTrustDialogAccepted"] is True
    )  # ...trust seeded...
    # ...launched with the container-local config dir, then the container is stopped on agent exit
    assert events == [f"launch:{tmp_path / '.claude'}", "on_exit"]


def test_main_resolves_the_adapter_from_the_agent_cli_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launcher holds no `claude` literal: it resolves the adapter by name and drives it. A fake
    # adapter proves the bootstrap-then-launch sequence runs against whatever `PANOPTICON_AGENT_CLI`
    # selects (default `claude`), and that the config dir derives from the adapter's config_dirname.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    calls: list[str] = []

    class _FakeCLI(ClaudeAgentCLI):
        name = "fake"
        config_dirname = ".fake"

        def auth_missing_detail(self, env: object, config_dir: object) -> str | None:
            calls.append("auth")
            return None

        def render_skills(self, client: object, task_id: str, home: Path) -> list[Path]:
            calls.append(f"skills:{home}")
            return []

        def render_operations(self, client: object, task_id: str, home: Path) -> list[Path]:
            calls.append("operations")
            return []

        def write_settings(self, home: Path) -> Path:
            calls.append("settings")
            return home

        def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
            calls.append(f"mcp:{config_dir}")
            return config_dir

        def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
            calls.append("overview")
            return None

        def trust_workspace(self, config_dir: Path, cwd: Path) -> Path:
            calls.append("trust")
            return config_dir

        def write_credentials(self, config_dir: Path, env: object) -> Path | None:
            calls.append("credentials")
            return None

        def launch(self, config_dir: Path) -> None:
            calls.append(f"launch:{config_dir}")

    agent.main(
        client_factory=lambda url: _FakeClient([]),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        agent_cli=_FakeCLI(),
        on_exit=lambda: calls.append("on_exit"),
    )
    # auth first, then the full bootstrap into <home>/.fake, then the adapter's own launch, then stop
    assert calls == [
        "auth",
        f"skills:{tmp_path}",
        "operations",
        "settings",
        f"mcp:{tmp_path / '.fake'}",
        "overview",
        "trust",
        "credentials",
        f"launch:{tmp_path / '.fake'}",
        "on_exit",
    ]


def test_main_fails_fast_when_no_auth_token_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    launched: list[str] = []
    fake = _FakeClient([])
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda cfg: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert launched == []  # launch must not be called
    assert len(fake.lifecycle_calls) == 1
    call = fake.lifecycle_calls[0]
    assert call["phase"] == "failed"
    assert call["runner_id"] == "runner-1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (call["detail"] or "")


def test_main_proceeds_when_anthropic_api_key_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    launched: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient([]),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda cfg: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert "launched" in launched  # ANTHROPIC_API_KEY alone is sufficient


def test_main_returns_early_without_lifecycle_call_when_runner_id_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.delenv("PANOPTICON_RUNNER_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    launched: list[str] = []
    fake = _FakeClient([])
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda cfg: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert launched == []  # still returns early without launching
    assert fake.lifecycle_calls == []  # no lifecycle call when runner_id absent
