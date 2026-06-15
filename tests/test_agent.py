"""The in-container agent launcher: the deterministic bootstrap (render the workflow's skills +
turn-flip hooks) then launch. No LLM — the real `claude` exec is a fake here."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.container import agent


class _FakeClient:
    def __init__(self, skills: list[dict[str, str]]) -> None:
        self._skills = skills

    def list_skills(self, task_id: str) -> list[dict[str, str]]:
        return self._skills


def test_render_skills_writes_command_files(tmp_path: Path) -> None:
    client = _FakeClient([{"name": "babysit-ci", "description": "Watch CI.", "instructions": "loop"}])
    agent.render_skills(client, "t1", tmp_path)  # type: ignore[arg-type]
    assert (tmp_path / ".claude" / "commands" / "babysit-ci.md").read_text().startswith("---\ndescription: Watch CI.")


def test_claude_argv_starts_fresh_without_a_session(tmp_path: Path) -> None:
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == ["claude"]


def test_claude_argv_continues_an_existing_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-work-repo"  # claude's <config>/projects/<cwd, / → ->
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}")
    assert agent._claude_argv(tmp_path, Path("/work/repo")) == ["claude", "--continue"]


def test_main_bootstraps_skills_and_hooks_then_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    launched: list[bool] = []
    agent.main(
        client_factory=lambda url: _FakeClient([{"name": "s", "description": "d", "instructions": "i"}]),  # type: ignore[arg-type,return-value]
        home=tmp_path,
        launch=lambda: launched.append(True),
    )
    assert (tmp_path / ".claude" / "commands" / "s.md").exists()  # skills rendered...
    assert (tmp_path / ".claude" / "settings.json").exists()  # ...turn-flip hooks written...
    assert launched == [True]  # ...then launched
