"""The in-container agent launcher: the deterministic bootstrap (render the workflow's skills +
turn-flip hooks, link credentials) then launch. No LLM — the real `claude` exec is a fake here."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.container import agent


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
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == ["claude"]


def test_claude_argv_continues_an_existing_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"  # claude's <config>/projects/<cwd, / → ->
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == ["claude", "--continue"]


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
    assert argv == ["claude", "--mcp-config", str(tmp_path / agent.MCP_CONFIG_FILE), "--strict-mcp-config"]


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


def test_main_bootstraps_into_a_container_local_config_dir_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    launched: list[Path] = []
    agent.main(
        client_factory=lambda url: _FakeClient(  # type: ignore[arg-type,return-value]
            [{"name": "s", "description": "d", "instructions": "i"}], {"advance": "COMPLETE"}
        ),
        home=tmp_path,
        launch=launched.append,
    )
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "s.md").exists()  # skills rendered...
    assert (commands / "advance.md").exists()  # ...operations rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert (tmp_path / ".claude" / agent.MCP_CONFIG_FILE).exists()  # ...MCP server wired...
    assert launched == [tmp_path / ".claude"]  # ...then launched with the container-local config dir
