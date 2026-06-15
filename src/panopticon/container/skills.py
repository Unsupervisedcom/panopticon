"""Render a workflow's :class:`~panopticon.core.models.Skill` specs to the claude CLI surface.

The Skill spec is agent-CLI-agnostic (core, ADR 0004); this is the **claude-specific renderer**
(M3 adds others): a skill becomes a `.claude/commands/<name>.md` slash-command the agent's CLI
picks up. Pure — no LLM; it just writes files. The in-container harness fetches the active
workflow's skills (over REST) and renders them before launching the agent (Slice 6c).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from panopticon.core.models import Skill


def render_command(skill: Skill) -> str:
    """The `.claude/commands/<name>.md` body for a skill: frontmatter + the agent procedure."""
    return f"---\ndescription: {skill.description}\n---\n{skill.instructions}\n"


def write_commands(skills: Iterable[Skill], root: Path) -> list[Path]:
    """Write each skill to ``<root>/.claude/commands/<name>.md``; return the paths written."""
    commands_dir = root / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for skill in skills:
        path = commands_dir / f"{skill.name}.md"
        path.write_text(render_command(skill))
        written.append(path)
    return written
