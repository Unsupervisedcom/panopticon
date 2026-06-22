"""Render the claude hook settings that wire the **turn-flip contract** (Slice 4):

- the agent's **Stop** hook flips the live turn to the *user* (the agent handed the ball back);
- **UserPromptSubmit** flips it back to the *agent* (the user replied).

claude-specific (`.claude/settings.json`); M3 revisits for other CLIs. Pure — the callback the
hooks invoke is :mod:`panopticon.container.hook`. `:blocked:` is preserved by construction: the
callback only sets the turn, never the block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panopticon.container.config import update_json_config

#: The command claude runs for each hook event (sets the turn via the task service).
HOOK_COMMAND = "python -m panopticon.container.hook"


def settings() -> dict[str, Any]:
    """The `.claude/settings.json` we seed: the turn-flip hooks **and** a pre-accept of Bypass
    Permissions mode.

    The agent launches with ``--dangerously-skip-permissions`` (no operator to answer prompts), but
    on a fresh config dir claude first stops on an interactive *"Bypass Permissions mode … 1. No,
    exit / 2. Yes, I accept"* gate — which hangs the container forever (the task shows "stuck
    starting"). claude records that acceptance as ``skipDangerousModePermissionPrompt`` in this file,
    so seeding it ``True`` up front pre-accepts the gate and claude goes straight to work.
    """

    def run(actor: str) -> dict[str, Any]:
        return {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} {actor}"}]}

    return {
        "hooks": {"Stop": [run("user")], "UserPromptSubmit": [run("agent")]},
        "skipDangerousModePermissionPrompt": True,
    }


def write_settings(home: Path) -> Path:
    """Merge the turn-flip hooks into ``<home>/.claude/settings.json``; return the path."""
    path = home / ".claude" / "settings.json"
    with update_json_config(path) as data:
        data.update(settings())
    return path
