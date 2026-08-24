"""The codex adapter (ADR 0014, M3.5): its rendered surface — prompts, the config.toml MCP + trust
blocks, the AGENTS.md overview, launch/resume argv, model tier, auth. No LLM — the real CLI exec
(:meth:`CodexAgentCLI.launch`) is never called here."""

from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path

from panopticon.container.cli.codex import CodexAgentCLI, _find_resume_target


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


def test_render_skills_writes_skill_files(tmp_path: Path) -> None:
    client = _FakeClient(
        [{"name": "babysit-ci", "description": "Watch CI.", "instructions": "loop"}]
    )
    CodexAgentCLI().render_skills(client, "t1", tmp_path)  # type: ignore[arg-type]
    # Model-discoverable skills surface; user scope keeps the working tree clean.
    path = tmp_path / ".agents" / "skills" / "babysit-ci" / "SKILL.md"
    assert path.exists(), f"expected SKILL.md at {path}"
    body = path.read_text()
    assert body.startswith("---\nname: babysit-ci\ndescription: Watch CI.")
    assert 'task_id="t1"' in body
    # Old custom-prompt surface must not be written.
    assert not (tmp_path / ".codex" / "prompts").exists()


def test_render_operations_writes_a_skill_per_operation(tmp_path: Path) -> None:
    client = _FakeClient([], {"advance": "COMPLETE", "drop": "DROPPED"})
    CodexAgentCLI().render_operations(client, "t1", tmp_path)  # type: ignore[arg-type]
    skills_dir = tmp_path / ".agents" / "skills"
    assert {p.parent.name for p in skills_dir.rglob("SKILL.md")} == {"advance", "drop"}
    body = (skills_dir / "advance" / "SKILL.md").read_text()
    assert body.startswith("---\nname: advance\n")
    assert "apply_operation" in body and "COMPLETE" in body
    assert 'task_id="t1"' in body
    # Old custom-prompt surface must not be written.
    assert not (tmp_path / ".codex" / "prompts").exists()


# -- MCP config (config.toml [mcp_servers.panopticon]) ------------------------------------------


def test_write_mcp_config_points_codex_at_the_task_service_over_http(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_mcp_config(tmp_path, "http://host.docker.internal:8000")
    assert path == tmp_path / cli.CONFIG_FILE
    data = _load_config(cli, tmp_path)
    assert data["mcp_servers"]["panopticon"] == {"url": "http://host.docker.internal:8000/mcp"}
    # older codex only picks up HTTP MCP with the rmcp client enabled (a no-op where it's native)
    assert data["features"]["experimental_use_rmcp_client"] is True
    # built-in apps connector cannot start in the container — disable it to avoid the 30 s stall
    assert data["features"]["apps"] is False


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


def test_overview_coexists_with_mcp_and_trust_in_one_config_toml(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    cli.write_workflow_overview(tmp_path, "# overview")
    cli.write_mcp_config(tmp_path, "http://svc:8000")
    cli.trust_workspace(tmp_path, Path("/workspace"))
    data = _load_config(cli, tmp_path)
    assert data["developer_instructions"] == "# overview"
    assert data["mcp_servers"]["panopticon"]["url"] == "http://svc:8000/mcp"
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"


# -- workflow overview → $CODEX_HOME/AGENTS.md --------------------------------------------------


def test_write_workflow_overview_injects_developer_instructions(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_workflow_overview(tmp_path, "# github-self-reviewed\nphases…")
    assert path == tmp_path / cli.CONFIG_FILE
    assert (
        _load_config(cli, tmp_path)["developer_instructions"] == "# github-self-reviewed\nphases…"
    )


def test_write_workflow_overview_skips_when_empty(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    assert cli.write_workflow_overview(tmp_path / "empty", "  ") is None


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


def test_auth_missing_detail_flags_the_absent_openai_key(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    detail = cli.auth_missing_detail({}, tmp_path)
    assert detail is not None
    # the detail names every accepted var so the operator knows what to set
    assert "OPENAI_API_KEY" in detail
    assert "CODEX_API_KEY" in detail and "CODEX_ACCESS_TOKEN" in detail


def test_auth_missing_detail_accepts_any_of_the_three_env_vars(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    assert cli.auth_missing_detail({"OPENAI_API_KEY": "sk"}, tmp_path) is None
    assert cli.auth_missing_detail({"CODEX_API_KEY": "sk"}, tmp_path) is None
    assert cli.auth_missing_detail({"CODEX_ACCESS_TOKEN": "tok"}, tmp_path) is None


def test_auth_missing_detail_accepts_a_pre_existing_auth_json(tmp_path: Path) -> None:
    # A container already logged in (auth.json on the per-task volume) must not be failed on a bare
    # env check — it would wrongly kill a container carried across respawn.
    cli = CodexAgentCLI()
    (tmp_path / cli.AUTH_FILE).write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}')
    assert cli.auth_missing_detail({}, tmp_path) is None


# -- credential materialization (auth.json + file cred store) -----------------------------------


def test_write_credentials_renders_auth_json_from_openai_api_key(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_credentials(tmp_path, {"OPENAI_API_KEY": "sk-abc"})
    assert path == tmp_path / cli.AUTH_FILE
    # exact shape `codex login --with-api-key` writes
    assert json.loads(path.read_text()) == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-abc"}
    assert (path.stat().st_mode & 0o777) == 0o600  # secret, owner-only
    # and the file credential store is pinned so codex never reaches for an (absent) keyring
    assert _load_config(cli, tmp_path)["cli_auth_credentials_store"] == "file"


def test_write_credentials_accepts_the_codex_api_key_spelling(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_credentials(tmp_path, {"CODEX_API_KEY": "sk-xyz"})
    assert path is not None
    assert json.loads(path.read_text())["OPENAI_API_KEY"] == "sk-xyz"


def test_write_credentials_never_clobbers_an_existing_auth_json(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    auth = tmp_path / cli.AUTH_FILE
    auth.write_text('{"auth_mode": "chatgpt", "tokens": "keep-me"}')
    assert cli.write_credentials(tmp_path, {"OPENAI_API_KEY": "sk-new"}) is None  # no write
    assert json.loads(auth.read_text()) == {"auth_mode": "chatgpt", "tokens": "keep-me"}
    # the cred-store pin is still applied so the existing login is read from file, not a keyring
    assert _load_config(cli, tmp_path)["cli_auth_credentials_store"] == "file"


def test_write_credentials_writes_no_auth_json_without_an_api_key(tmp_path: Path) -> None:
    # A workspace access token needs no file (codex reads it from the env); still pin the cred store.
    cli = CodexAgentCLI()
    assert cli.write_credentials(tmp_path, {"CODEX_ACCESS_TOKEN": "tok"}) is None
    assert not (tmp_path / cli.AUTH_FILE).exists()
    assert _load_config(cli, tmp_path)["cli_auth_credentials_store"] == "file"


def test_write_credentials_coexists_with_mcp_and_trust_in_one_config_toml(tmp_path: Path) -> None:
    # The cred-store key lands in the shared config.toml alongside the MCP/trust blocks.
    cli = CodexAgentCLI()
    cli.write_mcp_config(tmp_path, "http://svc:8000")
    cli.trust_workspace(tmp_path, Path("/workspace"))
    cli.write_credentials(tmp_path, {"OPENAI_API_KEY": "sk"})
    data = _load_config(cli, tmp_path)
    assert data["cli_auth_credentials_store"] == "file"
    assert data["mcp_servers"]["panopticon"]["url"] == "http://svc:8000/mcp"  # preserved
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"  # preserved


# -- model tier (ADR 0014 §3a) ------------------------------------------------------------------


def test_resolve_model_maps_the_primary_tier_to_a_codex_model() -> None:
    assert CodexAgentCLI().resolve_model("primary") == "gpt-5.6-sol"


def test_built_in_workflow_tier_resolves_to_a_concrete_codex_model() -> None:
    from panopticon.workflows.github_self_reviewed import GithubSelfReviewed

    assert CodexAgentCLI().resolve_model(GithubSelfReviewed.default_model) == "gpt-5.6-sol"


# -- launch / resume argv -----------------------------------------------------------------------


def test_launch_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    assert CodexAgentCLI().launch_argv(tmp_path, Path("/workspace")) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
    ]


def test_launch_argv_resumes_when_an_interactive_session_exists(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "2026" / "08"
    sessions.mkdir(parents=True)
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "sess-abc"}}'
    (sessions / "rollout-abc.jsonl").write_text(meta)
    assert CodexAgentCLI().launch_argv(tmp_path, Path("/workspace")) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "resume",
        "sess-abc",
    ]


def test_launch_argv_appends_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), initial_prompt="review plan")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "review plan",
    ]


def test_launch_argv_omits_initial_prompt_when_resuming(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "s1"}}'
    (tmp_path / "sessions" / "s.jsonl").write_text(meta)
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), initial_prompt="review plan")
    assert "resume" in argv and "review plan" not in argv


def test_launch_argv_passes_the_resolved_model_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "--model",
        "gpt-5.6-sol",
    ]


def test_launch_argv_omits_model_on_resume(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "s1"}}'
    (tmp_path / "sessions" / "s.jsonl").write_text(meta)
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary")
    assert "--model" not in argv and "resume" in argv


def test_launch_argv_passes_model_before_initial_prompt_on_first_run(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(
        tmp_path, Path("/workspace"), initial_prompt="start now", starting_model="primary"
    )
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "--model",
        "gpt-5.6-sol",
        "start now",
    ]


# -- _find_resume_target -----------------------------------------------------------------------


def _session_meta(
    session_id: str, originator: str = "codex-tui", thread_source: str = "user"
) -> str:
    """One-liner session_meta first-line JSON for tests."""
    import json

    return json.dumps(
        {"payload": {"originator": originator, "thread_source": thread_source, "id": session_id}}
    )


def test_find_resume_target_returns_none_when_no_sessions_dir(tmp_path: Path) -> None:
    assert _find_resume_target(tmp_path / "sessions") is None


def test_find_resume_target_returns_none_when_no_jsonl_files(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    assert _find_resume_target(tmp_path / "sessions") is None


def test_find_resume_target_skips_codex_exec_rollout(tmp_path: Path) -> None:
    # originator != "codex-tui" → not eligible
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "exec.jsonl").write_text(_session_meta("exec-1", originator="codex_exec"))
    assert _find_resume_target(d) is None


def test_find_resume_target_skips_subagent_thread(tmp_path: Path) -> None:
    # thread_source != "user" → internal subagent thread, not resumable
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "sub.jsonl").write_text(_session_meta("sub-1", thread_source="agent"))
    assert _find_resume_target(d) is None


def test_find_resume_target_skips_malformed_first_line(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "bad.jsonl").write_text("not json\n")
    assert _find_resume_target(d) is None


def test_find_resume_target_skips_empty_first_line(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "empty.jsonl").write_text("\n{}\n")  # empty first line
    assert _find_resume_target(d) is None


def test_find_resume_target_skips_bare_object_without_payload(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "bare.jsonl").write_text("{}")  # valid JSON, but no payload → skip
    assert _find_resume_target(d) is None


def test_find_resume_target_returns_id_of_interactive_session(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "sess.jsonl").write_text(_session_meta("interactive-1"))
    assert _find_resume_target(d) == "interactive-1"


def test_find_resume_target_newest_interactive_beats_older_exec(tmp_path: Path) -> None:
    # A newer exec rollout must not shadow an older interactive session.
    d = tmp_path / "sessions"
    d.mkdir()
    old = d / "old-interactive.jsonl"
    old.write_text(_session_meta("good-sess"))
    time.sleep(0.01)
    new = d / "new-exec.jsonl"
    new.write_text(_session_meta("exec-sess", originator="codex_exec"))
    # exec is newer by mtime but ineligible → interactive wins
    assert _find_resume_target(d) == "good-sess"


def test_find_resume_target_picks_newest_of_multiple_interactive(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    first = d / "first.jsonl"
    first.write_text(_session_meta("old-sess"))
    time.sleep(0.01)
    second = d / "second.jsonl"
    second.write_text(_session_meta("new-sess"))
    assert _find_resume_target(d) == "new-sess"


def test_find_resume_target_searches_subdirectories(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    sub = d / "2026" / "08"
    sub.mkdir(parents=True)
    (sub / "deep.jsonl").write_text(_session_meta("deep-sess"))
    assert _find_resume_target(d) == "deep-sess"


# -- launch_argv resume + interrupt prompt -------------------------------------------------------


def test_launch_argv_resumes_with_interrupt_prompt_when_agent_turn(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "s1"}}'
    (tmp_path / "sessions" / "s.jsonl").write_text(meta)
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), turn="agent")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "resume",
        "s1",
        "You were interrupted. Continue.",
    ]


def test_launch_argv_resumes_without_interrupt_prompt_when_user_turn(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "s1"}}'
    (tmp_path / "sessions" / "s.jsonl").write_text(meta)
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), turn="user")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "resume",
        "s1",
    ]


def test_launch_argv_falls_back_to_first_run_when_only_exec_sessions(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    exec_meta = '{"payload": {"originator": "codex_exec", "thread_source": "user", "id": "e1"}}'
    (tmp_path / "sessions" / "exec.jsonl").write_text(exec_meta)
    argv = CodexAgentCLI().launch_argv(
        tmp_path, Path("/workspace"), initial_prompt="hi", starting_model="primary"
    )
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "--model",
        "gpt-5.6-sol",
        "hi",
    ]


# -- reasoning-effort suffix --------------------------------------------------------------------


def test_launch_argv_passes_effort_config_when_starting_model_has_suffix(tmp_path: Path) -> None:
    # "primary:high" → --model gpt-5.6-sol --config model_reasoning_effort=high
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary:high")
    assert argv == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
        "--model",
        "gpt-5.6-sol",
        "--config",
        "model_reasoning_effort=high",
    ]


def test_launch_argv_passes_effort_config_for_raw_model_id_with_suffix(tmp_path: Path) -> None:
    # Raw model id with effort suffix — resolve_model passes the id through unchanged.
    argv = CodexAgentCLI().launch_argv(
        tmp_path, Path("/workspace"), starting_model="gpt-5.6-sol:medium"
    )
    assert "--model" in argv and "gpt-5.6-sol" in argv
    assert "--config" in argv and "model_reasoning_effort=medium" in argv


def test_launch_argv_omits_effort_config_without_suffix(tmp_path: Path) -> None:
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary")
    assert "--config" not in argv


def test_launch_argv_omits_effort_config_on_resume(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    meta = '{"payload": {"originator": "codex-tui", "thread_source": "user", "id": "s1"}}'
    (tmp_path / "sessions" / "s.jsonl").write_text(meta)
    argv = CodexAgentCLI().launch_argv(tmp_path, Path("/workspace"), starting_model="primary:high")
    assert "--config" not in argv and "resume" in argv


# -- hook seam (M3.6) ---------------------------------------------------------------------------


def test_has_live_background_task_always_false_for_codexs_stop_payload() -> None:
    # Codex's documented Stop payload carries no background-task array, so a real Stop flips the turn.
    cli = CodexAgentCLI()
    real_stop = {"hook_event_name": "Stop", "turn_id": "t", "stop_hook_active": False}
    assert cli.has_live_background_task(real_stop) is False
    assert cli.has_live_background_task({}) is False


# -- settings / hooks (M3.6) --------------------------------------------------------------------


def _hook_command(entry: object) -> str:
    # Unwrap codex's [[hooks.<Event>]] → [[hooks.<Event>.hooks]] → {type, command} nesting.
    assert isinstance(entry, list) and len(entry) == 1
    inner = entry[0]["hooks"]
    assert isinstance(inner, list) and len(inner) == 1 and inner[0]["type"] == "command"
    return str(inner[0]["command"])


def test_write_settings_wires_the_turn_flip_hooks(tmp_path: Path) -> None:
    cli = CodexAgentCLI()
    path = cli.write_settings(tmp_path)
    assert path == tmp_path / cli.config_dirname / cli.CONFIG_FILE
    data = tomllib.loads(path.read_text())
    hooks = data["hooks"]
    # Stop hands the ball to the user; UserPromptSubmit takes it back + prints briefing/nudge.
    assert _hook_command(hooks["Stop"]) == "python -m panopticon.container.hook user stop"
    assert (
        _hook_command(hooks["UserPromptSubmit"])
        == "python -m panopticon.container.hook agent prompt"
    )
    # No AskUserQuestion analogue in codex → no PreToolUse/PostToolUse pair (ADR 0014 flag 7).
    assert "PreToolUse" not in hooks and "PostToolUse" not in hooks


def test_hooks_coexist_with_mcp_and_trust_in_one_config_toml(tmp_path: Path) -> None:
    # The launcher calls all three against the same config.toml; none may clobber another's keys.
    cli = CodexAgentCLI()
    config_dir = tmp_path / cli.config_dirname
    cli.write_settings(tmp_path)  # takes home; the others take the config dir
    cli.write_mcp_config(config_dir, "http://svc:8000")
    cli.trust_workspace(config_dir, Path("/workspace"))
    data = tomllib.loads((config_dir / cli.CONFIG_FILE).read_text())
    assert _hook_command(data["hooks"]["Stop"]).endswith("user stop")  # preserved
    assert data["mcp_servers"]["panopticon"]["url"] == "http://svc:8000/mcp"
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"
