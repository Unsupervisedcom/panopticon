"""The claude harness — Anthropic's ``claude`` CLI as the (default) agent runtime.

Consolidates the claude-specific mechanics that used to live across ``container/agent.py``
(argv, MCP config, workflow overview, trust seeds), ``container/skills.py`` (slash-command
rendering), and ``container/hooks.py`` (turn-flip hook settings). Everything here is pure file
writes and argv computation; the launch happens in the agent launcher.

Auth is the ``CLAUDE_CODE_OAUTH_TOKEN`` env var the runner injects from the repo's ``env_file``
(or an ``ANTHROPIC_API_KEY``); see ``docs/auth.md``. The claude CLI itself ships in the base
image, so this harness contributes no image layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from panopticon.core.models import Skill
from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.config import update_json_config

#: claude's main config file. Holds (besides per-container state) per-project trust acceptance.
CONFIG_FILE = ".claude.json"

#: Filename of the rendered MCP client config in the config dir; claude is pointed at it via
#: ``--mcp-config`` so it connects to the task service's MCP server (task operations as tools).
MCP_CONFIG_FILE = "panopticon-mcp.json"
#: Filename of the rendered workflow overview (the whole-lifecycle map); claude gets its contents in
#: the system prompt via ``--append-system-prompt`` so the agent always knows the workflow's shape.
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"

#: The command claude runs for each hook event (sets the turn via the task service). The callback
#: module stays in ``container/`` — it runs inside the container and talks REST at hook time.
HOOK_COMMAND = "python -m panopticon.container.hook"


# The panopticon MCP tools all take a ``task_id`` (the server is shared across tasks). The agent
# can't read its container's env, so we inject the concrete id into each rendered command — the
# agent passes this verbatim. (Identity is a container-side fact; ARCHITECTURE §8.3.)
def _task_id_note(task_id: str) -> str:
    return (
        f'\nThis is task `{task_id}` — pass `task_id="{task_id}"` to every panopticon MCP tool '
        f"you call here.\n"
    )


def render_command(skill: Skill, task_id: str) -> str:
    """The `.claude/commands/<name>.md` body for a skill: frontmatter + the agent procedure."""
    return f"---\ndescription: {skill.description}\n---\n{skill.instructions}\n{_task_id_note(task_id)}"


def render_operation(name: str, target_state: str, task_id: str) -> str:
    """The `.claude/commands/<name>.md` body for a core operation (advance/drop/…).

    Operations are the workflow's **declared, gated** moves; the agent applies one by name via the
    `apply_operation` tool (not by editing state directly), which starts a new agentic turn.
    """
    return (
        f"---\ndescription: Apply the workflow's '{name}' operation.\n---\n"
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        f'Invoke it with the `apply_operation` tool (`operation="{name}"`, `task_id="{task_id}"`); '
        f"don't edit the state directly. It's gated on the current state's responsibilities and "
        f"starts a new turn.\n"
    )


def write_commands(skills: Iterable[Skill], root: Path, task_id: str) -> list[Path]:
    """Write each skill to ``<root>/.claude/commands/<name>.md``; return the paths written."""
    commands_dir = root / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for skill in skills:
        path = commands_dir / f"{skill.name}.md"
        path.write_text(render_command(skill, task_id))
        written.append(path)
    return written


def write_operation_commands(operations: Mapping[str, str], root: Path, task_id: str) -> list[Path]:
    """Write each core operation (verb → target state) to ``<root>/.claude/commands/<verb>.md``."""
    commands_dir = root / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, target_state in operations.items():
        path = commands_dir / f"{name}.md"
        path.write_text(render_operation(name, target_state, task_id))
        written.append(path)
    return written


def settings() -> dict[str, Any]:
    """The `.claude/settings.json` we seed: the turn-flip hooks **and** a pre-accept of Bypass
    Permissions mode.

    - the agent's **Stop** hook flips the live turn to the *user* (the agent handed the ball
      back) — *unless* a background task is still running, in which case the callback leaves the
      turn on the agent (see :mod:`panopticon.container.hook`);
    - **UserPromptSubmit** flips it back to the *agent* (the user replied);
    - **PreToolUse**/**PostToolUse** matched to ``AskUserQuestion`` flip to the *user* while the
      agent is asking the user something and back to the *agent* once it's answered.

    The agent launches with ``--dangerously-skip-permissions`` (no operator to answer prompts), but
    on a fresh config dir claude first stops on an interactive *"Bypass Permissions mode … 1. No,
    exit / 2. Yes, I accept"* gate — which hangs the container forever (the task shows "stuck
    starting"). claude records that acceptance as ``skipDangerousModePermissionPrompt`` in this file,
    so seeding it ``True`` up front pre-accepts the gate and claude goes straight to work.
    """

    def run(actor: str, event: str | None = None, *, matcher: str | None = None) -> dict[str, Any]:
        # `actor` is the turn to set; the optional `event` selects the callback's side-effect
        # (briefing on the prompt hook, token report on stop) — the bare question hooks pass none.
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
    """Merge the turn-flip hooks into ``<home>/.claude/settings.json``; return the path."""
    path = home / ".claude" / "settings.json"
    with update_json_config(path) as data:
        data.update(settings())
    return path


def write_mcp_config(config_dir: Path, service_url: str) -> Path:
    """Write claude's MCP client config so it connects to the task service's MCP server.

    A single ``panopticon`` HTTP server at ``<service_url>/mcp`` — the same control plane the
    container already polls (``PANOPTICON_SERVICE_URL``, the in-container view). Returns the path,
    which the launcher passes to ``claude --mcp-config``."""
    import json

    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / MCP_CONFIG_FILE
    server = {"type": "http", "url": f"{service_url.rstrip('/')}/mcp"}
    path.write_text(json.dumps({"mcpServers": {"panopticon": server}}, indent=2))
    return path


def write_workflow_overview(config_dir: Path, overview: str) -> Path | None:
    """Write the whole-workflow map so the launcher can put it in claude's system prompt. Returns the
    path, or ``None`` when there's no overview (skipped — the agent just gets the per-turn briefing)."""
    if not overview.strip():
        return None
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / WORKFLOW_OVERVIEW_FILE
    path.write_text(overview)
    return path


def trust_workspace(config_dir: Path, cwd: Path) -> Path:
    """Pre-accept claude's first-run dialogs for ``cwd``.

    Three blockers fire on a fresh container and must be pre-seeded — there is no operator in the
    container to dismiss them interactively:

    - ``hasCompletedOnboarding`` — the general onboarding screen.
    - ``projects[<cwd>].hasTrustDialogAccepted`` — "Do you trust the files in this folder?"
      (cf. claude issue #45298; separate from ``--dangerously-skip-permissions``).
    - ``hasAcknowledgedCostThreshold`` — cost-acknowledgment dialog shown when authenticating
      via ``ANTHROPIC_API_KEY`` (not shown for OAuth tokens).

    Merge-in-place so we don't clobber config claude writes itself, and idempotent. The path
    encoding is undocumented internals — a safe degradation if it ever drifts is that the dialog
    reappears, which only matters in an (already attended) interactive re-attach.
    """
    config = config_dir / CONFIG_FILE
    with update_json_config(config) as data:
        data["hasCompletedOnboarding"] = True
        data["hasAcknowledgedCostThreshold"] = True
        projects = data.setdefault("projects", {})
        projects.setdefault(str(cwd), {})["hasTrustDialogAccepted"] = True
    return config


class ClaudeHarness(Harness):
    """The default harness: ``claude`` with the surface Slice 6 built, behind the M3 interface."""

    name: ClassVar[str] = "claude"
    config_dirname: ClassVar[str] = ".claude"

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        if environ.get("CLAUDE_CODE_OAUTH_TOKEN") or environ.get("ANTHROPIC_API_KEY"):
            return None
        return (
            "No auth token — set CLAUDE_CODE_OAUTH_TOKEN in the repo's env_file (see docs/auth.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        write_commands(ctx.skills, ctx.home, ctx.task_id)
        write_operation_commands(ctx.operations, ctx.home, ctx.task_id)  # advance/drop/…
        write_settings(ctx.home)  # turn-flip hooks → <home>/.claude/settings.json
        write_mcp_config(config_dir, ctx.service_url)  # point claude at the task service's MCP
        write_workflow_overview(config_dir, ctx.overview)  # → system prompt (the map)
        trust_workspace(config_dir, ctx.cwd)  # pre-accept the trust dialog (no operator to)

    def argv(self, ctx: LaunchContext) -> list[str]:
        """`claude` argv, resuming the project's most recent conversation if one exists.

        The agent runs unattended in a throwaway container on a per-task clone, so it launches with
        ``--dangerously-skip-permissions`` — there's no operator to answer permission prompts, and
        the blast radius is the task's own checkout. claude keeps per-project transcripts under
        ``<config>/projects/<cwd with '/' → '-'>``; when one is there we ``--continue`` it instead
        of starting fresh. The config dir is a **per-task volume**, so this resumes both within a
        container's life and **across respawn/recreate**. If our path encoding ever misses
        claude's, we simply start fresh — a safe degradation.

        On a **first run** (no prior session) with an ``initial_prompt``, the prompt is appended as
        a positional argument so claude processes it immediately. On a **resumed session**
        (``--continue``) the ``initial_prompt`` is omitted — the agent is already mid-task. When
        the resumed session is the agent's turn, :data:`INTERRUPT_PROMPT` is appended instead so
        the agent automatically picks up where it left off rather than waiting for user input.

        ``starting_model`` (e.g. ``"opus"``) is passed as ``--model`` on the **first run only** —
        on resume claude uses whichever model the conversation was already using.
        """
        config_dir = self.config_dir(ctx.home)
        argv = ["claude", "--dangerously-skip-permissions"]
        overview = config_dir / WORKFLOW_OVERVIEW_FILE
        if overview.exists():  # the whole-workflow map → claude's system prompt
            argv += ["--append-system-prompt", overview.read_text()]
        mcp_config = config_dir / MCP_CONFIG_FILE
        if mcp_config.exists():  # connect to the task service's MCP server, and *only* it
            argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        project = config_dir / "projects" / str(ctx.cwd).replace("/", "-")
        if any(project.glob("*.jsonl")):
            argv.append("--continue")
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)  # positional: auto-resume after container restart
        else:
            if (
                ctx.starting_model
            ):  # first run only — on resume claude uses the conversation's existing model
                argv += ["--model", ctx.starting_model]
            if ctx.initial_prompt:
                argv.append(
                    ctx.initial_prompt
                )  # positional: claude sends this as the agent's first message
        return argv

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.config_dir(ctx.home))}
