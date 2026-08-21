"""The codex adapter (ADR 0014, M3.5): its rendered surface — prompts, the config.toml MCP + trust
blocks, the AGENTS.md overview, launch/resume argv, model tier, auth. No LLM — the real CLI exec
(:meth:`CodexAgentCLI.launch`) is never called here."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

from panopticon.container.cli.codex import CodexAgentCLI


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


def _load_config(cli: CodexAgentCLI, config_dir: Path) -> dict[str, object]:
    return tomllib.loads((config_dir / cli.CONFIG_FILE).read_text())


# -- skills + operations → custom prompts -------------------------------------------------------


def test_render_skills_writes_prompt_files(tmp_path: Path) -> None:
    client = _FakeClient(
        [{"name": "babysit-ci", "description": "Watch CI.", "instructions": "loop"}]
    )
    CodexAgentCLI().render_skills(client, "t1", tmp_path)  # type: ignore[arg-type]
    body = (tmp_path / ".codex" / "prompts" / "babysit-ci.md").read_text()
    # Same frontmatter format codex + claude both read; the task id is injected for MCP calls.
    assert body.startswith("---\ndescription: Watch CI.")
    assert 'task_id="t1"' in body


def test_render_operations_writes_a_prompt_per_operation(tmp_path: Path) -> None:
    client = _FakeClient([], {"advance": "COMPLETE", "drop": "DROPPED"})
    CodexAgentCLI().render_operations(client, "t1", tmp_path)  # type: ignore[arg-type]
    prompts = tmp_path / ".codex" / "prompts"
    assert {p.name for p in prompts.glob("*.md")} == {"advance.md", "drop.md"}
    body = (prompts / "advance.md").read_text()
    assert "apply_operation" in body and "COMPLETE" in body
    assert 'task_id="t1"' in body


# -- MCP config (config.toml [mcp_servers.panopticon]) ------------------------------------------


def test_write_mcp_config_points_codex_at_the_task_service_over_http(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_mcp_config(tmp_path, "http://host.docker.internal:8000")
    assert path == tmp_path / cli.CONFIG_FILE
    data = _load_config(cli, tmp_path)
    assert data["mcp_servers"]["panopticon"] == {"url": "http://host.docker.internal:8000/mcp"}
    # older codex only picks up HTTP MCP with the rmcp client enabled (a no-op where it's native)
    assert data["features"]["experimental_use_rmcp_client"] is True


def test_write_mcp_config_strips_a_trailing_slash(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    cli.write_mcp_config(tmp_path, "http://svc:8000/")
    assert _load_config(cli, tmp_path)["mcp_servers"]["panopticon"]["url"] == "http://svc:8000/mcp"


def test_mcp_and_trust_coexist_in_one_config_toml(tmp_path: Path) -> None:
    # The launcher calls both; neither must clobber the other's keys in the shared config.toml.
    cli = CodexAgentCLI()
    cli.write_mcp_config(tmp_path, "http://svc:8000")
    cli.trust_workspace(tmp_path, Path("/workspace"))
    data = _load_config(cli, tmp_path)
    assert data["mcp_servers"]["panopticon"]["url"] == "http://svc:8000/mcp"  # preserved
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"
    assert data["approval_policy"] == "never"


# -- workflow overview → $CODEX_HOME/AGENTS.md --------------------------------------------------


def test_write_workflow_overview_writes_agents_md_else_skips(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_workflow_overview(tmp_path, "# github-self-reviewed\nphases…")
    assert path == tmp_path / "AGENTS.md" and path.read_text() == "# github-self-reviewed\nphases…"
    assert cli.write_workflow_overview(tmp_path / "empty", "  ") is None  # no overview → skipped


# -- trust / unattended posture -----------------------------------------------------------------


def test_trust_workspace_seeds_trust_and_unattended_posture(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    cli.trust_workspace(tmp_path, Path("/workspace"))
    data = _load_config(cli, tmp_path)
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"
    # persisted (not just a launch flag) so resume stays unattended too (codex issue #9144)
    assert data["approval_policy"] == "never"
    assert data["sandbox_mode"] == "danger-full-access"


def test_trust_workspace_merges_and_is_idempotent(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    config = tmp_path / cli.CONFIG_FILE
    # codex already wrote config (incl. another trusted project) — we must not clobber it.
    config.write_text('[projects."/other"]\ntrust_level = "trusted"\n')
    cli.trust_workspace(tmp_path, Path("/workspace"))
    cli.trust_workspace(tmp_path, Path("/workspace"))  # idempotent
    data = _load_config(cli, tmp_path)
    assert data["projects"]["/other"]["trust_level"] == "trusted"  # preserved
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"


# -- auth env check -----------------------------------------------------------------------------


def test_auth_missing_detail_flags_the_absent_openai_key() -> None:
    cli = CodexAgentCLI()
    assert cli.auth_missing_detail({}) is not None
    assert "OPENAI_API_KEY" in (cli.auth_missing_detail({}) or "")
    assert cli.auth_missing_detail({"OPENAI_API_KEY": "sk"}) is None


# -- model tier (ADR 0014 §3a) ------------------------------------------------------------------


def test_resolve_model_maps_the_primary_tier_to_a_codex_model() -> None:
    assert CodexAgentCLI().resolve_model("primary") == "gpt-5.6-codex"


def test_resolve_model_passes_unknown_values_through() -> None:
    assert CodexAgentCLI().resolve_model("gpt-5.6") == "gpt-5.6"


def test_built_in_workflow_tier_resolves_to_a_concrete_codex_model() -> None:
    from panopticon.workflows.github_self_reviewed import GithubSelfReviewed

    assert CodexAgentCLI().resolve_model(GithubSelfReviewed.default_model) == "gpt-5.6-codex"


# -- launch / resume argv -----------------------------------------------------------------------


def test_launch_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    assert CodexAgentCLI().launch_argv(tmp_path, Path("/workspace")) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_launch_argv_resumes_when_a_session_transcript_exists(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "2026" / "08"
    sessions.mkdir(parents=True)
    (sessions / "rollout-abc.jsonl").write_text("{}")
    assert CodexAgentCLI().launch_argv(tmp_path, Path("/workspace")) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "resume",
        "--last",
    ]


def test_launch_argv_appends_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), initial_prompt="review plan")
    assert argv == ["codex", "--dangerously-bypass-approvals-and-sandbox", "review plan"]


def test_launch_argv_omits_initial_prompt_when_resuming(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s.jsonl").write_text("{}")
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), initial_prompt="review plan")
    assert "resume" in argv and "review plan" not in argv


def test_launch_argv_passes_the_resolved_model_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5.6-codex",
    ]


def test_launch_argv_omits_model_on_resume(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s.jsonl").write_text("{}")
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary")
    assert "--model" not in argv and "resume" in argv


def test_launch_argv_passes_model_before_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(
        tmp_path, Path("/workspace"), initial_prompt="start now", starting_model="primary"
    )
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5.6-codex",
        "start now",
    ]


# -- hook seam (M3.6 stubs; concrete + safe here) -----------------------------------------------


def test_read_hook_payload_tolerates_empty_and_invalid() -> None:
    cli = CodexAgentCLI()
    assert cli.read_hook_payload(io.StringIO("")) == {}
    assert cli.read_hook_payload(io.StringIO("not json")) == {}
    assert cli.read_hook_payload(io.StringIO("[]")) == {}  # JSON, but not an object
    assert cli.read_hook_payload(io.StringIO('{"a": 1}')) == {"a": 1}


def test_has_live_background_task_degrades_to_false_until_m36() -> None:
    # Codex's background-task payload shape (ADR flag 2) is wired in M3.6; until then the turn flips
    # on Stop (the same degradation claude uses when the field is absent).
    cli = CodexAgentCLI()
    assert cli.has_live_background_task({}) is False
    assert cli.has_live_background_task({"background_tasks": [{"status": "running"}]}) is False


# -- settings / hooks (M3.6) --------------------------------------------------------------------


def test_write_settings_returns_the_config_path_without_wiring_hooks_yet(tmp_path: Path) -> None:
    # M3.5 keeps the class concrete; the actual turn-flip hook block is M3.6.
    cli = CodexAgentCLI()
    path = cli.write_settings(tmp_path)
    assert path == tmp_path / cli.config_dirname / cli.CONFIG_FILE
    assert path.parent.is_dir()  # config dir ensured for the other writers
