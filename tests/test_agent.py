"""The in-container agent launcher: the deterministic bootstrap (render the workflow's skills +
turn-flip hooks, link credentials) then launch. No LLM — the real `claude` exec is a fake here."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.container import agent


class _FakeClient:
    def __init__(
        self, skills: list[dict[str, str]], operations: dict[str, str] | None = None,
        overview: str = "# the workflow",
    ) -> None:
        self._skills = skills
        self._operations = operations or {}
        self._overview = overview

    def list_skills(self, task_id: str) -> list[dict[str, str]]:
        return self._skills

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def workflow_overview(self, task_id: str) -> str:
        return self._overview


def test_render_skills_writes_command_files(tmp_path: Path) -> None:
    client = _FakeClient([{"name": "babysit-ci", "description": "Watch CI.", "instructions": "loop"}])
    agent.render_skills(client, "t1", tmp_path)  # type: ignore[arg-type]
    assert (tmp_path / ".claude" / "commands" / "babysit-ci.md").read_text().startswith("---\ndescription: Watch CI.")


def test_render_operations_writes_a_command_per_operation(tmp_path: Path) -> None:
    client = _FakeClient([], {"advance": "COMPLETE", "drop": "DROPPED"})
    agent.render_operations(client, "t1", tmp_path)  # type: ignore[arg-type]
    commands = tmp_path / ".claude" / "commands"
    assert {p.name for p in commands.glob("*.md")} == {"advance.md", "drop.md"}
    body = (commands / "advance.md").read_text()
    assert "apply_operation" in body and "COMPLETE" in body  # tells the agent how + the target
    assert 'task_id="t1"' in body  # the container's task id, injected for the MCP tool call


def test_claude_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    # Unattended container, per-task clone → skip permission prompts (no operator to answer them).
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == ["claude", "--dangerously-skip-permissions"]


def test_claude_argv_continues_an_existing_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"  # claude's <config>/projects/<cwd, / → ->
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
    ]


def test_write_mcp_config_points_claude_at_the_task_service_mcp(tmp_path: Path) -> None:
    import json

    path = agent.write_mcp_config(tmp_path, "http://host.docker.internal:8000")
    assert path == tmp_path / agent.MCP_CONFIG_FILE
    cfg = json.loads(path.read_text())
    server = cfg["mcpServers"]["panopticon"]
    assert server == {"type": "http", "url": "http://host.docker.internal:8000/mcp"}


def test_claude_argv_adds_strict_mcp_config_when_present(tmp_path: Path) -> None:
    agent.write_mcp_config(tmp_path, "http://svc:8000")
    argv = agent._claude_argv(tmp_path, Path("/work/repo"))
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--mcp-config",
        str(tmp_path / agent.MCP_CONFIG_FILE),
        "--strict-mcp-config",
    ]


def test_write_workflow_overview_writes_the_map_else_skips(tmp_path: Path) -> None:
    path = agent.write_workflow_overview(tmp_path, "# github-peer-reviewed\nphases…")
    assert path == tmp_path / agent.WORKFLOW_OVERVIEW_FILE and path.read_text() == "# github-peer-reviewed\nphases…"
    assert agent.write_workflow_overview(tmp_path / "empty", "  ") is None  # no overview → skipped


def test_claude_argv_appends_the_workflow_overview_to_the_system_prompt(tmp_path: Path) -> None:
    agent.write_workflow_overview(tmp_path, "# the workflow map")
    argv = agent._claude_argv(tmp_path, Path("/work/repo"))
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == "# the workflow map"  # the map's contents go inline into the system prompt


def test_link_credentials_symlinks_only_the_credential_file(tmp_path: Path) -> None:
    # The per-repo creds volume holds only `.credentials.json`; it's symlinked into the
    # container-local config dir so the token is shared but other claude state is not.
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text("{token}")
    config_dir = tmp_path / "home" / ".claude"

    agent.link_credentials(config_dir, creds_dir=creds_dir)

    link = config_dir / ".credentials.json"
    assert link.is_symlink() and link.resolve() == (creds_dir / ".credentials.json").resolve()
    assert link.read_text() == "{token}"  # refreshes write through to the shared volume


def test_link_credentials_is_a_noop_without_a_logged_in_volume(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    agent.link_credentials(config_dir, creds_dir=tmp_path / "empty")  # no creds yet
    assert config_dir.is_dir() and not (config_dir / ".credentials.json").exists()


def test_trust_workspace_seeds_acceptance_for_a_fresh_config(tmp_path: Path) -> None:
    import json

    config_dir = tmp_path / ".claude"
    agent.trust_workspace(config_dir, Path("/workspace"))
    data = json.loads((config_dir / ".claude.json").read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True  # the other first-run blocker


def test_trust_workspace_merges_and_is_idempotent(tmp_path: Path) -> None:
    import json

    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    # claude already wrote config (incl. an existing project) — we must not clobber it.
    (config_dir / ".claude.json").write_text(
        json.dumps({"userID": "u", "projects": {"/other": {"history": []}}})
    )
    agent.trust_workspace(config_dir, Path("/workspace"))
    agent.trust_workspace(config_dir, Path("/workspace"))  # idempotent
    data = json.loads((config_dir / ".claude.json").read_text())
    assert data["userID"] == "u"  # preserved
    assert data["projects"]["/other"] == {"history": []}  # preserved
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


def test_seed_account_copies_only_identity_fields(tmp_path: Path) -> None:
    import json

    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    (creds_dir / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"uuid": "acc-1"}, "userID": "u-1", "hasCompletedOnboarding": True,
        "projects": {"/creds": {}}, "mcpServers": {"x": {}},  # container-local cruft, must NOT leak
    }))
    config_dir = tmp_path / ".claude"

    agent.seed_account(config_dir, creds_dir=creds_dir)

    seeded = json.loads((config_dir / ".claude.json").read_text())
    assert seeded["oauthAccount"] == {"uuid": "acc-1"} and seeded["userID"] == "u-1"
    assert seeded["hasCompletedOnboarding"] is True
    assert "projects" not in seeded and "mcpServers" not in seeded  # only identity is shared


def test_seed_account_is_a_noop_without_a_logged_in_volume(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    agent.seed_account(config_dir, creds_dir=tmp_path / "empty")  # no creds config
    assert not (config_dir / ".claude.json").exists()  # nothing seeded → claude handles login


def test_refresh_credentials_links_and_seeds_from_a_populated_volume(tmp_path: Path) -> None:
    # The runner execs this into a running container after `login` writes the volume, so a live
    # agent that started logged-out gets the link + account without a respawn.
    import json

    from panopticon.container import refresh_credentials

    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text("{token}")
    (creds_dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"uuid": "a"}, "userID": "u"}))
    config_dir = tmp_path / "home" / ".claude"  # container-local, started without creds

    refresh_credentials.main(config_dir, creds_dir=creds_dir)

    link = config_dir / ".credentials.json"
    assert link.is_symlink() and link.read_text() == "{token}"  # token reads live through the link
    seeded = json.loads((config_dir / ".claude.json").read_text())
    assert seeded["oauthAccount"] == {"uuid": "a"} and seeded["userID"] == "u"  # account seeded


def test_refresh_credentials_is_a_noop_without_a_logged_in_volume(tmp_path: Path) -> None:
    from panopticon.container import refresh_credentials

    config_dir = tmp_path / ".claude"
    refresh_credentials.main(config_dir, creds_dir=tmp_path / "empty")  # nothing logged in yet
    assert config_dir.is_dir() and not (config_dir / ".credentials.json").exists()
    assert not (config_dir / ".claude.json").exists()


def test_main_bootstraps_into_a_container_local_config_dir_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
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
    assert (tmp_path / ".claude" / agent.MCP_CONFIG_FILE).exists()  # ...MCP server wired...
    assert (tmp_path / ".claude" / agent.WORKFLOW_OVERVIEW_FILE).exists()  # ...workflow map written...
    import json

    trust = json.loads((tmp_path / ".claude" / ".claude.json").read_text())
    assert trust["projects"][str(Path.cwd())]["hasTrustDialogAccepted"] is True  # ...trust seeded...
    # ...launched with the container-local config dir, then the container is stopped on agent exit
    assert events == [f"launch:{tmp_path / '.claude'}", "on_exit"]
