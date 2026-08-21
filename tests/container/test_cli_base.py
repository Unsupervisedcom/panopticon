"""The agent-CLI adapter seam (ADR 0014): the ABC + the name-keyed registry. A CLI drops in by
implementing :class:`AgentCLI` and registering under its name, with no launcher edit. The claude
adapter's own behavior lives in :mod:`tests.container.test_claude`."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.container.cli import (
    DEFAULT_AGENT_CLI,
    AgentCLI,
    get_agent_cli,
    register_agent_cli,
)
from panopticon.container.cli.claude import ClaudeAgentCLI
from panopticon.container.cli.codex import CodexAgentCLI


def test_default_resolves_to_claude() -> None:
    assert DEFAULT_AGENT_CLI == "claude"
    assert isinstance(get_agent_cli(), ClaudeAgentCLI)  # no name → the default
    assert isinstance(get_agent_cli("claude"), ClaudeAgentCLI)


def test_codex_is_a_registered_built_in_adapter() -> None:
    # The second built-in CLI: registering it makes it resolvable with no launcher edit (ADR 0014 §2).
    assert isinstance(get_agent_cli("codex"), CodexAgentCLI)


def test_unknown_cli_name_is_a_clear_error() -> None:
    with pytest.raises(KeyError, match="unknown agent CLI 'nope'"):
        get_agent_cli("nope")


def test_registering_an_adapter_makes_it_resolvable_without_a_launcher_edit() -> None:
    class _Fake(AgentCLI):
        name = "fake-cli"
        config_dirname = ".fake"

        def render_skills(self, client: object, task_id: str, home: Path) -> list[Path]:
            return []

        def render_operations(self, client: object, task_id: str, home: Path) -> list[Path]:
            return []

        def write_settings(self, home: Path) -> Path:
            return home

        def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
            return config_dir

        def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
            return None

        def trust_workspace(self, config_dir: Path, cwd: Path) -> Path:
            return config_dir

        def auth_missing_detail(self, env: object) -> str | None:
            return None

        def resolve_model(self, tier: str) -> str:
            return tier

        def read_hook_payload(self, stdin: object) -> dict[str, object]:
            return {}

        def has_live_background_task(self, payload: dict[str, object]) -> bool:
            return False

        def launch(self, config_dir: Path) -> None:  # pragma: no cover - not exercised
            pass

    register_agent_cli(_Fake)
    resolved = get_agent_cli("fake-cli")
    assert isinstance(resolved, _Fake) and resolved.config_dirname == ".fake"
