"""Render the claude hook settings that wire the **turn-flip contract** (Slice 4):

- the agent's **Stop** hook flips the live turn to the *user* (the agent handed the ball back) —
  *unless* a background task is still running, in which case the callback leaves the turn on the
  agent (see :mod:`panopticon.container.hook`), since the task's completion re-invokes the agent
  without a UserPromptSubmit;
- **UserPromptSubmit** flips it back to the *agent* (the user replied);
- **PreToolUse**/**PostToolUse** matched to the ``AskUserQuestion`` tool flip to the *user* while
  the agent is asking the user something (so the dashboard shows input is required) and back to the
  *agent* once it's answered. ``AskUserQuestion`` is a mid-turn tool call — it never fires ``Stop``
  — so without this the turn would wrongly read *agent* the whole time the question is pending.

It also seeds the two *non-hook* settings a fresh container needs: the Bypass Permissions
pre-accept (see :func:`settings`) and the starting :data:`THEME`.

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

#: The theme a fresh container's claude starts in. ``auto`` follows the terminal's background, so a
#: pane an operator attaches over `tmux`/`ssh` is readable on a light *or* dark terminal — claude's
#: own default is a fixed ``dark``. claude has no ``--theme`` flag, so this is config-file state:
#: ``theme`` is a *user setting*, i.e. it lives in the settings file :func:`write_settings` writes
#: (:meth:`~panopticon.container.cli.claude.ClaudeAgentCLI.trust_workspace` seeds the same value
#: into claude's legacy global config for older builds that read it from there).
THEME = "auto"


def settings() -> dict[str, Any]:
    """The `.claude/settings.json` we seed: the turn-flip hooks **and** a pre-accept of Bypass
    Permissions mode.

    The agent launches with ``--dangerously-skip-permissions`` (no operator to answer prompts), but
    on a fresh config dir claude first stops on an interactive *"Bypass Permissions mode … 1. No,
    exit / 2. Yes, I accept"* gate — which hangs the container forever (the task shows "stuck
    starting"). claude records that acceptance as ``skipDangerousModePermissionPrompt`` in this file,
    so seeding it ``True`` up front pre-accepts the gate and claude goes straight to work.
    """

    def run(actor: str, event: str | None = None, *, matcher: str | None = None) -> dict[str, Any]:
        # `actor` is the turn to set; the optional `event` selects the callback's side-effect
        # (briefing on the prompt hook; background-task gating of the turn flip on stop) — the bare question hooks pass none.
        command = f"{HOOK_COMMAND} {actor}" + (f" {event}" if event else "")
        entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
        if (
            matcher is not None
        ):  # PreToolUse/PostToolUse are tool-scoped; Stop/UserPromptSubmit aren't
            entry["matcher"] = matcher
        return entry

    return {
        "hooks": {
            "Stop": [run("user", "stop")],
            "UserPromptSubmit": [run("agent", "prompt")],
            # The agent stops to ask the user → flip to user; once answered → back to agent.
            "PreToolUse": [run("user", matcher="AskUserQuestion")],
            "PostToolUse": [run("agent", matcher="AskUserQuestion")],
        },
        "skipDangerousModePermissionPrompt": True,
    }


def write_settings(home: Path) -> Path:
    """Merge the turn-flip hooks into ``<home>/.claude/settings.json``; return the path.

    :data:`THEME` is seeded rather than enforced: the config dir is a **per-task volume** that
    outlives a respawn, and the ask is what a claude instance *starts* in — so a deliberate
    ``/theme`` change made inside the container survives every later launch, while the hooks and the
    permission pre-accept (which the container can't work without) are re-applied each time.
    """
    path = home / ".claude" / "settings.json"
    with update_json_config(path) as data:
        data.update(settings())
        data.setdefault("theme", THEME)
    return path
