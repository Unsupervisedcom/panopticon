"""The claude adapter (ADR 0014) reproducing today's rendered surface byte-for-byte: argv, MCP
config, workflow overview, trust, model tier, and hook-payload parsing. No LLM — the real CLI exec
(:meth:`ClaudeAgentCLI.launch`) is never called here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.container.cli.claude import INTERRUPT_PROMPT, ClaudeAgentCLI
from panopticon.container.hooks import THEME


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


def test_resolve_model_maps_the_primary_tier_to_opus() -> None:
    # The control plane stores an abstract tier; the claude adapter is the only place it becomes a
    # provider model name (ADR 0014 §3a).
    assert ClaudeAgentCLI().resolve_model("primary") == "opus"


def test_built_in_workflow_tier_resolves_to_a_concrete_claude_model() -> None:
    # End to end across the two halves: the tier the control plane declares (never a model name)
    # resolves through the claude adapter to today's concrete model.
    from panopticon.workflows.github_self_reviewed import GithubSelfReviewed

    assert ClaudeAgentCLI().resolve_model(GithubSelfReviewed.default_model) == "opus"


def test_launch_argv_passes_the_resolved_model_on_first_run(tmp_path: Path) -> None:
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), starting_model="primary")
    assert argv == ["claude", "--dangerously-skip-permissions", "--model", "opus"]


def test_launch_argv_omits_model_on_resume(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    argv = ClaudeAgentCLI().launch_argv(tmp_path, Path("/work/repo"), starting_model="primary")
    assert "--model" not in argv
    assert "--continue" in argv


def test_launch_argv_passes_model_before_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = ClaudeAgentCLI().launch_argv(
        tmp_path, Path("/work/repo"), initial_prompt="start now", starting_model="primary"
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
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {})
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True
    assert data["hasAcknowledgedCostThreshold"] is True  # suppresses the API-key cost dialog
    assert data["theme"] == THEME  # claude's legacy home for the starting theme


def test_trust_workspace_keeps_a_theme_the_container_already_chose(tmp_path: Path) -> None:
    # Seeded, not enforced — a `/theme` run inside the container survives the next launch.
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(json.dumps({"theme": "light"}))
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {})
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert data["theme"] == "light"
    assert data["hasCompletedOnboarding"] is True  # the pre-accepts are still applied


def test_trust_workspace_merges_and_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    # claude already wrote config (incl. an existing project) — we must not clobber it.
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(
        json.dumps({"userID": "u", "projects": {"/other": {"history": []}}})
    )
    cli = ClaudeAgentCLI()
    cli.trust_workspace(config_dir, Path("/workspace"), {})
    cli.trust_workspace(config_dir, Path("/workspace"), {})  # idempotent
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert data["userID"] == "u"  # preserved
    assert data["projects"]["/other"] == {"history": []}  # preserved
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


# -- API-key approval pre-accept -----------------------------------------------------------------
#
# claude gates a bare ANTHROPIC_API_KEY behind "Detected a custom API key in your environment / Do
# you want to use this API key?" — default No, and the same `customApiKeyResponses.approved` list
# also decides whether the key is *usable* at all. Unattended, nobody can answer it. The approval
# token is claude's own `$ve(key) = key.trim().slice(-20)`.

_KEY = "sk-ant-api03-0123456789abcdefghijklmnop"
_TRUNCATED = _KEY[-20:]


def _responses(config_dir: Path) -> dict[str, list[str]]:
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    responses: dict[str, list[str]] = data.get(ClaudeAgentCLI.API_KEY_RESPONSES, {})
    return responses


def test_trust_workspace_pre_approves_the_env_api_key(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {"ANTHROPIC_API_KEY": _KEY})
    assert _responses(config_dir)["approved"] == [_TRUNCATED]
    # Only the 20-char suffix is persisted — exactly what claude stores when a human answers Yes.
    assert _KEY not in (config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text()


@pytest.mark.parametrize(
    "raw",
    [
        f'"{_KEY}"',  # ANTHROPIC_API_KEY="sk-ant-…" in the env-file
        f"'{_KEY}'",
        f"  {_KEY}  ",
        f"{_KEY}\r",  # CRLF-terminated env-file
        f'"{_KEY}"\r\n',
    ],
)
def test_trust_workspace_approves_the_normalized_key(tmp_path: Path, raw: str) -> None:
    # The regression that matters: docker's --env-file keeps the quotes, and `launch` hands claude
    # the *normalized* value — so the seed must be the normalized truncation, or claude computes a
    # different token at startup and the dialog comes back.
    config_dir = tmp_path / ".claude"
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {"ANTHROPIC_API_KEY": raw})
    assert _responses(config_dir)["approved"] == [_TRUNCATED]


def test_trust_workspace_approval_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    cli = ClaudeAgentCLI()
    env = {"ANTHROPIC_API_KEY": _KEY}
    cli.trust_workspace(config_dir, Path("/workspace"), env)
    cli.trust_workspace(config_dir, Path("/workspace"), env)  # a respawn re-runs the bootstrap
    assert _responses(config_dir)["approved"] == [_TRUNCATED]  # not duplicated


def test_trust_workspace_unwedges_a_previously_rejected_key(tmp_path: Path) -> None:
    # An operator attach that answered No (or a dialog that timed out) would otherwise persist a
    # rejection that survives every respawn, leaving the task permanently unauthenticated.
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(
        json.dumps({ClaudeAgentCLI.API_KEY_RESPONSES: {"approved": [], "rejected": [_TRUNCATED]}})
    )
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {"ANTHROPIC_API_KEY": _KEY})
    assert _responses(config_dir) == {"approved": [_TRUNCATED], "rejected": []}


def test_trust_workspace_preserves_unrelated_key_responses(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(
        json.dumps(
            {ClaudeAgentCLI.API_KEY_RESPONSES: {"approved": ["other-a"], "rejected": ["other-r"]}}
        )
    )
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {"ANTHROPIC_API_KEY": _KEY})
    assert _responses(config_dir) == {
        "approved": ["other-a", _TRUNCATED],
        "rejected": ["other-r"],  # another key's rejection is none of our business
    }


def test_trust_workspace_approves_a_short_key_whole(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {"ANTHROPIC_API_KEY": "short"})
    assert _responses(config_dir)["approved"] == ["short"]  # slice(-20) of a shorter key is itself


@pytest.mark.parametrize("env", [{}, {"ANTHROPIC_API_KEY": ""}, {"ANTHROPIC_API_KEY": '""'}])
def test_trust_workspace_seeds_no_approval_without_a_key(
    tmp_path: Path, env: dict[str, str]
) -> None:
    # The OAuth-token path: no dialog fires, so the config stays exactly as it was before.
    config_dir = tmp_path / ".claude"
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), env)
    data = json.loads((config_dir / ClaudeAgentCLI.CONFIG_FILE).read_text())
    assert ClaudeAgentCLI.API_KEY_RESPONSES not in data
    assert data["hasCompletedOnboarding"] is True  # the other pre-accepts still applied


def test_trust_workspace_leaves_an_existing_approval_list_alone_without_a_key(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    existing = {"approved": ["other-a"], "rejected": ["other-r"]}
    (config_dir / ClaudeAgentCLI.CONFIG_FILE).write_text(
        json.dumps({ClaudeAgentCLI.API_KEY_RESPONSES: existing})
    )
    ClaudeAgentCLI().trust_workspace(config_dir, Path("/workspace"), {})
    assert _responses(config_dir) == existing


# -- auth env check -----------------------------------------------------------------------------


def test_auth_missing_detail_flags_the_absent_token(tmp_path: Path) -> None:
    cli = ClaudeAgentCLI()
    assert cli.auth_missing_detail({}, tmp_path) is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (cli.auth_missing_detail({}, tmp_path) or "")
    assert cli.auth_missing_detail({"CLAUDE_CODE_OAUTH_TOKEN": "sk"}, tmp_path) is None
    assert cli.auth_missing_detail({"ANTHROPIC_API_KEY": "sk"}, tmp_path) is None  # either suffices


def test_auth_missing_detail_reads_through_env_file_quoting(tmp_path: Path) -> None:
    cli = ClaudeAgentCLI()
    assert cli.auth_missing_detail({"CLAUDE_CODE_OAUTH_TOKEN": '"sk"'}, tmp_path) is None
    assert cli.auth_missing_detail({"ANTHROPIC_API_KEY": "'sk'"}, tmp_path) is None
    # `KEY=""` is a truthy two-character string, but the operator wrote "no credential".
    assert cli.auth_missing_detail({"CLAUDE_CODE_OAUTH_TOKEN": '""'}, tmp_path) is not None
    assert cli.auth_missing_detail({"ANTHROPIC_API_KEY": "  "}, tmp_path) is not None


def test_launch_env_normalizes_claudes_credentials() -> None:
    # What `launch` merges over the process env — so claude, and the `gh` the forge skills shell
    # out to, see the value the operator meant.
    assert ClaudeAgentCLI().launch_env({"ANTHROPIC_API_KEY": f'"{_KEY}"'}) == {
        "ANTHROPIC_API_KEY": _KEY
    }
    assert ClaudeAgentCLI().launch_env({"GH_TOKEN": '"ghp_x"'}) == {"GH_TOKEN": "ghp_x"}


def test_write_credentials_is_a_no_op_for_claude(tmp_path: Path) -> None:
    # claude reads its token from the env; there's no on-disk credential to materialize.
    assert ClaudeAgentCLI().write_credentials(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "sk"}) is None


# -- hook payload seam (background-task gating) --------------------------------------------------


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
