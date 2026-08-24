"""The agent-CLI adapter seam (ADR 0014): the ABC + the name-keyed registry. A CLI drops in by
implementing :class:`AgentCLI` and registering under its name, with no launcher edit. Shared
base-class behavior (read_hook_payload, resolve_model passthrough) lives here; per-adapter
MODEL_TIERS mapping assertions live in their own modules."""

from __future__ import annotations

import io
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
        MODEL_TIERS = {}

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

        def auth_missing_detail(self, env: object, config_dir: object) -> str | None:
            return None

        def write_credentials(self, config_dir: Path, env: object) -> Path | None:
            return None

        def has_live_background_task(self, payload: dict[str, object]) -> bool:
            return False

        def launch(self, config_dir: Path) -> None:  # pragma: no cover - not exercised
            pass

    register_agent_cli(_Fake)
    resolved = get_agent_cli("fake-cli")
    assert isinstance(resolved, _Fake) and resolved.config_dirname == ".fake"


# -- shared base-class behaviour ----------------------------------------------------------------


def test_read_hook_payload_tolerates_empty_and_invalid() -> None:
    # Shared implementation on the base — tested once; adapter tests cover only their own seams.
    cli = ClaudeAgentCLI()
    assert cli.read_hook_payload(io.StringIO("")) == {}
    assert cli.read_hook_payload(io.StringIO("not json")) == {}
    assert cli.read_hook_payload(io.StringIO("[]")) == {}  # JSON, but not an object
    assert cli.read_hook_payload(io.StringIO('{"a": 1}')) == {"a": 1}


def test_resolve_model_passes_unknown_tiers_through() -> None:
    # The passthrough fallback lives on the base; adapters supply only their own mapping.
    assert ClaudeAgentCLI().resolve_model("some-raw-model-id") == "some-raw-model-id"


def test_resolve_model_rejects_an_unmapped_reserved_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reserved tier absent from the adapter's MODEL_TIERS must fail loud — the stale-image
    # backstop (resolve_tier raises instead of leaking the raw tier to --model).
    monkeypatch.setattr(ClaudeAgentCLI, "MODEL_TIERS", {})
    with pytest.raises(ValueError, match="primary"):
        ClaudeAgentCLI().resolve_model("primary")
