"""The claude adapter (ADR 0014) reproducing today's rendered surface byte-for-byte: argv, MCP
config, workflow overview, trust, model tier, and hook-payload parsing. No LLM — the real CLI exec
(:meth:`ClaudeAgentCLI.launch`) is never called here."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from panopticon.container.claude import INTERRUPT_PROMPT, ClaudeAgentCLI


class _FakeClient:
    def __init__(
        self, skills: list[dict[str, str]], operations: dict[str, str] | None = None
    ) -> None:
        self._skills = skills
        self._operations = operations or {}

    def list_skills(self, task_id: str) -> list[dict[str, str]]:
        return self._skills

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations


# -- skills + operations ------------------------------------------------------------------------


def test_render_skills_writes_command_files(tmp_path: Path) -> None:
    client = _FakeClient(
        [{"name": "babysit-ci", "description": "Watch CI.", "instructions": "loop"}]
    )
    ClaudeAgentCLI().render_skills(client, "t1", tmp_path)  # type: ignore[arg-type]
    assert (
        (tmp_path / ".claude" / "commands" / "babysit-ci.md")
        .read_text()
        .startswith("---\ndescription: Watch CI.")
    )


def test_render_operations_writes_a_command_per_operation(tmp_path: Path) -> None:
    client = _FakeClient([], {"advance": "COMPLETE", "drop": "DROPPED"})
    ClaudeAgentCLI().render_operations(client, "t1", tmp_path)  # type: ignore[arg-type]
    commands = tmp_path / ".claude" / "commands"
    assert {p.name for p in commands.glob("*.md")} == {"advance.md", "drop.md"}
    body = (commands / "advance.md").read_text()
    assert "apply_operation" in body and "COMPLETE" in body  # tells the agent how + the target
    assert 'task_id="t1"' in body  # the container's task id, injected for the MCP tool call


# -- launch argv --------------------------------------------------------------------------------


def test_launch_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    # Unattended container, per-task clone → skip permission prompts (no operator to answer them).
    assert ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo")) == [
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_launch_argv_continues_an_existing_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"  # claude's <config>/projects/<cwd, / → ->
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    assert ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo")) == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
    ]


def test_launch_argv_appends_initial_prompt_on_first_session(tmp_path: Path) -> None:
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), initial_prompt="review plan")
    assert argv == ["claude", "--dangerously-skip-permissions", "review plan"]


def test_launch_argv_omits_initial_prompt_when_continuing_a_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), initial_prompt="review plan")
    assert "--continue" in argv
    assert "review plan" not in argv


def test_launch_argv_appends_interrupt_prompt_on_respawn_for_agent_turn(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), turn="agent")
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
        INTERRUPT_PROMPT,
    ]


def test_launch_argv_omits_interrupt_prompt_on_respawn_for_user_turn(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), turn="user")
    assert argv == ["claude", "--dangerously-skip-permissions", "--continue"]


def test_launch_argv_adds_strict_mcp_config_when_present(tmp_path: Path) -> None:
    cli = ClaudeAgentCLI()
    cli.write_mcp_config(tmp_path, "http://svc:8000")
    argv = cli.launch_argv(tmp_path, Path("/work/repo"))
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--mcp-config",
        str(tmp_path / ClaudeAgentCLI.MCP_CONFIG_FILE),
        "--strict-mcp-config",
    ]


def test_launch_argv_appends_the_workflow_overview_to_the_system_prompt(tmp_path: Path) -> None:
    cli = ClaudeAgentCLI()
    cli.write_workflow_overview(tmp_path, "# the workflow map")
    argv = cli.launch_argv(tmp_path, Path("/work/repo"))
    i = argv.index("--append-system-prompt")
    assert (
        argv[i + 1] == "# the workflow map"
    )  # the map's contents go inline into the system prompt


# -- model tier (ADR 0014 §3a) ------------------------------------------------------------------


def test_resolve_model_is_identity_for_claude() -> None:
    # claude's --model takes the tier vocabulary directly, so the mapping is the identity today; the
    # seam is what lets another CLI map the same tier to its own model id.
    assert ClaudeAgentCLI().resolve_model("opus") == "opus"


def test_launch_argv_passes_the_resolved_model_on_first_run(tmp_path: Path) -> None:
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), starting_model="opus")
    assert argv == ["claude", "--dangerously-skip-permissions", "--model", "opus"]


def test_launch_argv_omits_model_on_resume(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), starting_model="opus")
    assert "--model" not in argv
    assert "--continue" in argv


def test_launch_argv_passes_model_before_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = ClaudeAgentCLI().launch_argv(
        tmp_path, Path("/work/repo"), initial_prompt="start now", starting_model="opus"
    )
    assert argv == ["claude", "--dangerously-skip-permissions", "--model", "opus", "start now"]


# -- MCP config + workflow overview -------------------------------------------------------------


def test_write_mcp_config_points_claude_at_the_task_service_mcp(tmp_path: Path) -> None:
    path = ClaudeAgentCLI().write_mcp_config(tmp_path, "http://host.docker.internal:8000")
    assert path == tmp_path / ClaudeAgentCLI.MCP_CONFIG_FILE
    server = json.loads(path.read_text())["mcpServers"]["panopticon"]
    assert server == {"type": "http", "url": "http://host.docker.internal:8000/mcp"}


def test_write_workflow_overview_writes_the_map_else_skips(tmp_path: Path) -> None:
    cli = ClaudeAgentCLI()
    path = cli.write_workflow_overview(tmp_path, "# github-peer-reviewed\nphases…")
    assert (
        path == tmp_path / ClaudeAgentCLI.WORKFLOW_OVERVIEW_FILE
        and path.read_text() == "# github-peer-reviewed\nphases…"
    )
    assert cli.write_workflow_overview(tmp_path / "empty", "  ") is None  # no overview → skipped


# -- trust pre-accept ---------------------------------------------------------------------------


def test_trust_workspace_seeds_acceptance_for_a_fresh_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"))
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True
    assert data["hasAcknowledgedCostThreshold"] is True  # suppresses the API-key cost dialog


def test_trust_workspace_merges_and_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    # claude already wrote config (incl. an existing project) — we must not clobber it.
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(
        json.dumps({"userID": "u", "projects": {"/other": {"history": []}}})
    )
    cli = ClaudeAgentCLI()
    cli.trust_workspace(config_dir, Path("/workspace"))
    cli.trust_workspace(config_dir, Path("/workspace"))  # idempotent
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert data["userID"] == "u"  # preserved
    assert data["projects"]["/other"] == {"history": []}  # preserved
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


# -- auth env check -----------------------------------------------------------------------------


def test_auth_missing_detail_flags_the_absent_token() -> None:
    cli = ClaudeAgentCLI()
    assert cli.auth_missing_detail({}) is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (cli.auth_missing_detail({}) or "")
    assert cli.auth_missing_detail({"CLAUDE_CODE_OAUTH_TOKEN": "sk"}) is None
    assert cli.auth_missing_detail({"ANTHROPIC_API_KEY": "sk"}) is None  # either is sufficient


# -- hook payload seam (background-task gating) --------------------------------------------------


def test_read_hook_payload_tolerates_empty_and_invalid() -> None:
    cli = ClaudeAgentCLI()
    assert cli.read_hook_payload(io.StringIO("")) == {}
    assert cli.read_hook_payload(io.StringIO("not json")) == {}
    assert cli.read_hook_payload(io.StringIO("[]")) == {}  # JSON, but not an object
    assert cli.read_hook_payload(io.StringIO('{"a": 1}')) == {"a": 1}


@pytest.mark.parametrize(
    "payload,live",
    [
        ({"background_tasks": [{"id": "t", "status": "running"}]}, True),
        ({"background_tasks": [{"id": "t"}]}, True),  # no status → conservative
        ({"background_tasks": [{"id": "t", "status": "completed"}, {"status": "running"}]}, True),
        ({"background_tasks": [{"id": "t", "status": "completed"}]}, False),
        ({"background_tasks": [{"id": "t", "status": "FAILED"}]}, False),  # case-insensitive
        ({"background_tasks": []}, False),
        ({"background_tasks": "oops"}, False),  # wrong type → degrade
        ({}, False),
    ],
)
def test_has_live_background_task(payload: dict[str, object], live: bool) -> None:
    assert ClaudeAgentCLI().has_live_background_task(payload) is live
