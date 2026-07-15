"""The in-container agent launcher: fetch the workflow surface, dispatch to the task's harness
(the deterministic bootstrap), then launch. No LLM — the real CLI launch is a fake here."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.container import agent
from panopticon.harnesses import Harness, LaunchContext
from panopticon.harnesses.claude import MCP_CONFIG_FILE, WORKFLOW_OVERVIEW_FILE


class _FakeClient:
    def __init__(
        self,
        skills: list[dict[str, str]] | None = None,
        operations: dict[str, str] | None = None,
        overview: str = "# the workflow",
    ) -> None:
        self._skills = skills or []
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


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    for var in (
        "PANOPTICON_HARNESS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "PANOPTICON_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_main_bootstraps_the_default_claude_harness_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-test")
    events: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(  # type: ignore[arg-type,return-value]
            [{"name": "s", "description": "d", "instructions": "i"}], {"advance": "COMPLETE"}
        ),
        home=tmp_path,
        launch=lambda harness, ctx: events.append(f"launch:{harness.name}"),
        on_exit=lambda: events.append("on_exit"),
    )
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "s.md").exists()  # skills rendered...
    assert (commands / "advance.md").exists()  # ...operations rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert (tmp_path / ".claude" / MCP_CONFIG_FILE).exists()  # ...MCP server wired...
    assert (tmp_path / ".claude" / WORKFLOW_OVERVIEW_FILE).exists()  # ...workflow map written...
    # ...launched with the claude harness, then the container is stopped on agent exit
    assert events == ["launch:claude", "on_exit"]


def test_main_passes_the_launch_context_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-test")
    monkeypatch.setenv("PANOPTICON_INITIAL_PROMPT", "review your plan")
    monkeypatch.setenv("PANOPTICON_TASK_TURN", "agent")
    monkeypatch.setenv("PANOPTICON_STARTING_MODEL", "opus")
    seen: list[LaunchContext] = []
    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: seen.append(ctx),
        on_exit=lambda: None,
    )
    (ctx,) = seen
    assert ctx.initial_prompt == "review your plan"
    assert ctx.turn == "agent"
    assert ctx.starting_model == "opus"
    assert ctx.home == tmp_path


def test_main_fails_fast_when_no_auth_token_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner-1")
    launched: list[str] = []
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
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
    _base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    launched: list[str] = []
    agent.main(
        client_factory=lambda url: _FakeClient(),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert "launched" in launched  # ANTHROPIC_API_KEY alone is sufficient


def test_main_returns_early_without_lifecycle_call_when_runner_id_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("PANOPTICON_RUNNER_ID", raising=False)
    launched: list[str] = []
    fake = _FakeClient()
    agent.main(
        client_factory=lambda url: fake,  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda harness, ctx: launched.append("launched"),
        on_exit=lambda: launched.append("on_exit"),
    )
    assert launched == []  # still returns early without launching
    assert fake.lifecycle_calls == []  # no lifecycle call when runner_id absent


def test_run_agent_merges_the_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The launch runs the harness argv with the harness env layered over the container's.
    recorded: dict[str, object] = {}

    class _FakeHarness(Harness):
        name = "fake"
        config_dirname = ".fake"

        def missing_auth(self, environ: object, *, home: Path) -> str | None:
            return None

        def bootstrap(self, ctx: object) -> None:
            pass

        def argv(self, ctx: LaunchContext) -> list[str]:
            return ["fake-cli", "--go"]

        def env(self, ctx: LaunchContext) -> dict[str, str]:
            return {"FAKE_HOME": "/f"}

    def fake_run(argv: list[str], env: dict[str, str] | None = None) -> None:
        recorded["argv"] = argv
        recorded["env"] = env

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_agent(_FakeHarness(), LaunchContext(home=Path("/h"), cwd=Path("/w")))
    assert recorded["argv"] == ["fake-cli", "--go"]
    env = recorded["env"]
    assert isinstance(env, dict) and env["FAKE_HOME"] == "/f"
