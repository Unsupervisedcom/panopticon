"""Render a workflow's :class:`~panopticon.core.models.Skill` specs to an agent CLI's command surface.

The Skill spec is agent-CLI-agnostic (core, ADR 0004). The rendered body (frontmatter + the agent
procedure) is shared across CLIs; only the destination and frontmatter format differ:

- **claude** — ``.claude/commands/<name>.md``, ``---\\ndescription: …\\n---`` frontmatter.
- **codex** — ``~/.agents/skills/<name>/SKILL.md``, ``---\\nname: …\\ndescription: …\\n---``
  frontmatter (codex's model-discoverable skills mechanism; written to user scope so nothing reaches
  the task's working tree).

Pure — no LLM; it just writes files. The in-container harness fetches the active workflow's skills
(over REST) and renders them before launching the agent (Slice 6c).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from panopticon.core.models import Skill

#: The default destination (relative to the config home) — claude's slash-command dir.
CLAUDE_COMMANDS_SUBDIR: tuple[str, ...] = (".claude", "commands")


# The panopticon MCP tools all take a ``task_id`` (the server is shared across tasks). The agent
# can't read its container's env, so we inject the concrete id into each rendered command — the
# agent passes this verbatim. (Identity is a container-side fact; ARCHITECTURE §8.3.)
def _task_id_note(task_id: str) -> str:
    return (
        f'\nThis is task `{task_id}` — pass `task_id="{task_id}"` to every panopticon MCP tool '
        f"you call here.\n"
    )


def render_command(skill: Skill, task_id: str) -> str:
    """The rendered ``<name>.md`` body for a skill: frontmatter + the agent procedure.

    CLI-agnostic — both adapters write this same text; only the destination dir differs.
    """
    return f"---\ndescription: {skill.description}\n---\n{skill.instructions}\n{_task_id_note(task_id)}"


def render_operation(name: str, target_state: str, task_id: str) -> str:
    """The rendered ``<name>.md`` body for a core operation (advance/drop/…).

    CLI-agnostic — both adapters write this same text; only the destination dir differs.
    Operations are the workflow's **declared, gated** moves; the agent applies one by name via the
    `apply_operation` tool (not by editing state directly), then follows the returned briefing.
    """
    return (
        f"---\ndescription: Apply the workflow's '{name}' operation.\n---\n"
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        f'Invoke it with the `apply_operation` tool (`operation="{name}"`, `task_id="{task_id}"`); '
        f"don't edit the state directly. It's gated on the current state's responsibilities and "
        f"returns the entered phase's briefing. If the new phase is nonterminal and you hold its "
        f"turn, follow that briefing and continue immediately.\n"
    )


def write_commands(
    skills: Iterable[Skill],
    root: Path,
    task_id: str,
    subdir: Sequence[str] = CLAUDE_COMMANDS_SUBDIR,
) -> list[Path]:
    """Write each skill to ``<root>/<subdir>/<name>.md``; return the paths written.

    ``subdir`` defaults to claude's ``.claude/commands`` (byte-for-byte as before); codex passes
    ``(".codex", "prompts")``."""
    commands_dir = root.joinpath(*subdir)
    commands_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for skill in skills:
        path = commands_dir / f"{skill.name}.md"
        path.write_text(render_command(skill, task_id))
        written.append(path)
    return written


def write_operation_commands(
    operations: Mapping[str, str],
    root: Path,
    task_id: str,
    subdir: Sequence[str] = CLAUDE_COMMANDS_SUBDIR,
) -> list[Path]:
    """Write each core operation (verb → target state) to ``<root>/<subdir>/<verb>.md``."""
    commands_dir = root.joinpath(*subdir)
    commands_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, target_state in operations.items():
        path = commands_dir / f"{name}.md"
        path.write_text(render_operation(name, target_state, task_id))
        written.append(path)
    return written


# -- codex skills surface (model-discoverable; ~/.agents/skills/<name>/SKILL.md) ----------------


def render_agent_skill(skill: Skill, task_id: str) -> str:
    """The rendered ``SKILL.md`` body for a skill on the codex agent-skills surface.

    Adds ``name:`` to the frontmatter (required by the skills mechanism) alongside ``description:``.
    The instructions body and task-id note are the same as :func:`render_command`.
    """
    return (
        f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n"
        f"{skill.instructions}\n{_task_id_note(task_id)}"
    )


def render_agent_operation(name: str, target_state: str, task_id: str) -> str:
    """The rendered ``SKILL.md`` body for a core operation on the codex agent-skills surface."""
    return (
        f"---\nname: {name}\ndescription: Apply the workflow's '{name}' operation.\n---\n"
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        f'Invoke it with the `apply_operation` tool (`operation="{name}"`, `task_id="{task_id}"`); '
        f"don't edit the state directly. It's gated on the current state's responsibilities and "
        f"returns the entered phase's briefing. If the new phase is nonterminal and you hold its "
        f"turn, follow that briefing and continue immediately.\n"
    )


def write_agent_skills(skills: Iterable[Skill], root: Path, task_id: str) -> list[Path]:
    """Write each skill to ``<root>/.agents/skills/<name>/SKILL.md``; return the paths written.

    Uses codex's model-discoverable skills mechanism. Written to user scope (``<root>`` is
    ``~``), so nothing reaches the task's working tree.
    """
    written = []
    for skill in skills:
        skill_dir = root / ".agents" / "skills" / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(render_agent_skill(skill, task_id))
        written.append(path)
    return written


def write_agent_operation_skills(
    operations: Mapping[str, str], root: Path, task_id: str
) -> list[Path]:
    """Write each core operation to ``<root>/.agents/skills/<name>/SKILL.md``."""
    written = []
    for name, target_state in operations.items():
        skill_dir = root / ".agents" / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(render_agent_operation(name, target_state, task_id))
        written.append(path)
    return written
