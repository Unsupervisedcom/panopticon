"""Render the claude hook settings that wire the **turn-flip contract** (Slice 4):

- the agent's **Stop** hook flips the live turn to the *user* (the agent handed the ball back);
- **UserPromptSubmit** flips it back to the *agent* (the user replied).

claude-specific (`.claude/settings.json`); M3 revisits for other CLIs. Pure — the callback the
hooks invoke is :mod:`panopticon.container.hook`. `:blocked:` is preserved by construction: the
callback only sets the turn, never the block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The command claude runs for each hook event (sets the turn via the task service).
HOOK_COMMAND = "python -m panopticon.container.hook"


def settings() -> dict[str, Any]:
    """The `.claude/settings.json` hook block for turn-flip tracking."""

    def run(actor: str) -> dict[str, Any]:
        return {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} {actor}"}]}

    return {"hooks": {"Stop": [run("user")], "UserPromptSubmit": [run("agent")]}}


def write_settings(home: Path) -> Path:
    """Write the turn-flip hooks to ``<home>/.claude/settings.json``; return the path."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "settings.json"
    path.write_text(json.dumps(settings(), indent=2))
    return path
