"""Rendering workflow Skill specs to the claude CLI surface (`.claude/commands/<name>.md`)."""

from __future__ import annotations

from pathlib import Path

from panopticon.container.skills import render_command, render_operation, write_commands
from panopticon.core.models import Skill


def test_render_command_is_frontmatter_plus_procedure_with_task_id() -> None:
    skill = Skill(name="babysit-ci", description="Watch CI and fix failures.", instructions="Do X, then Y.")
    body = render_command(skill, "t-1")
    assert body.startswith("---\ndescription: Watch CI and fix failures.\n---\nDo X, then Y.\n")
    assert 'task_id="t-1"' in body  # the concrete task id is injected for MCP tool calls


def test_render_operation_injects_the_task_id() -> None:
    body = render_operation("advance", "COMPLETE", "t-9")
    assert "apply_operation" in body and "COMPLETE" in body
    assert 'operation="advance"' in body and 'task_id="t-9"' in body


def test_write_commands_writes_one_file_per_skill(tmp_path: Path) -> None:
    skills = [
        Skill("babysit-ci", "Watch CI.", "watch loop"),
        Skill("babysit-merge", "Shepherd the merge.", "merge loop"),
    ]
    paths = write_commands(skills, tmp_path, "t-1")
    assert {p.name for p in paths} == {"babysit-ci.md", "babysit-merge.md"}
    body = (tmp_path / ".claude" / "commands" / "babysit-ci.md").read_text()
    assert body.startswith("---\ndescription: Watch CI.\n---\n") and "watch loop" in body
    assert 'task_id="t-1"' in body
