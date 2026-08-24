"""The `claude` :class:`~panopticon.container.cli.base.AgentCLI` adapter (ADR 0014).

Every ``container/`` seam claude satisfies today, unchanged in effect: the rendered artifacts
(``.claude/commands/*``, ``.claude/settings.json``, the MCP config, the launch argv, the trust
file) are **byte-for-byte** what the launcher produced before the seam existed — this adapter only
selects claude's renderers and paths, delegating to the pure renderers (:mod:`skills`, :mod:`hooks`,
:mod:`config`) that stay reusable across CLIs. A second CLI (Codex) is a sibling module implementing
the same ABC.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, TextIO

from panopticon.container.cli.base import AgentCLI, _Client, resolve_tier
from panopticon.container.config import update_json_config
from panopticon.container.hooks import write_settings
from panopticon.container.skills import write_commands, write_operation_commands
from panopticon.core.models import Skill

#: A background task's ``status`` counts as *finished* only if it's one of these; anything else —
#: including a missing/unknown status — is treated as live, so we err toward keeping the turn on the
#: agent rather than handing it back prematurely.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "error"})

#: Sent to claude as the first message when a container restarts mid-task on the agent's turn.
INTERRUPT_PROMPT = "You were interrupted. Continue."

#: The control plane's abstract model **tiers** mapped to claude's concrete ``--model`` ids (ADR
#: 0014 §3a). This is the only place a provider model name appears; ``core``/``workflows`` name only
#: the tier. Unknown values pass through unchanged (see ``resolve_model``).
_MODEL_TIERS = {"primary": "opus"}


class ClaudeAgentCLI(AgentCLI):
    """The `claude` adapter — every ``container/`` seam claude satisfies today, unchanged in effect."""

    name = "claude"
    config_dirname = ".claude"

    #: claude's main config file. Holds (besides per-container state) per-project trust acceptance.
    CONFIG_FILE: ClassVar[str] = ".claude.json"
    #: The rendered MCP client config; claude is pointed at it via ``--mcp-config``.
    MCP_CONFIG_FILE: ClassVar[str] = "panopticon-mcp.json"
    #: The rendered workflow overview; its contents go to claude via ``--append-system-prompt``.
    WORKFLOW_OVERVIEW_FILE: ClassVar[str] = "workflow-overview.md"

    def render_skills(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's skills to `.claude/commands/` (the claude slash-command surface)."""
        skills = [Skill(**s) for s in client.list_skills(task_id)]
        return write_commands(skills, home, task_id)

    def render_operations(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's declared core operations (advance/drop/…) as slash-commands.

        Reflects the *active workflow's* declared moves (ADR 0004), so different workflows expose
        different operation commands — not a fixed global menu.
        """
        return write_operation_commands(client.list_operations(task_id), home, task_id)

    def write_settings(self, home: Path) -> Path:
        """Merge the turn-flip hooks into ``<home>/.claude/settings.json``; return the path."""
        return write_settings(home)

    def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
        """Write claude's MCP client config so it connects to the task service's MCP server.

        A single ``panopticon`` HTTP server at ``<service_url>/mcp`` — the same control plane the
        container already polls. Returns the path, which :meth:`launch` passes to ``--mcp-config``.
        """
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / self.MCP_CONFIG_FILE
        server = {"type": "http", "url": f"{service_url.rstrip('/')}/mcp"}
        path.write_text(json.dumps({"mcpServers": {"panopticon": server}}, indent=2))
        return path

    def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
        """Write the whole-workflow map so :meth:`launch` can put it in claude's system prompt.
        Returns the path, or ``None`` when there's no overview (the agent just gets the briefing)."""
        if not overview.strip():
            return None
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / self.WORKFLOW_OVERVIEW_FILE
        path.write_text(overview)
        return path

    def trust_workspace(self, config_dir: Path, cwd: Path) -> Path:
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
        config = config_dir / self.CONFIG_FILE
        with update_json_config(config) as data:
            data["hasCompletedOnboarding"] = True
            data["hasAcknowledgedCostThreshold"] = True
            projects = data.setdefault("projects", {})
            projects.setdefault(str(cwd), {})["hasTrustDialogAccepted"] = True
        return config

    def auth_missing_detail(self, env: Mapping[str, str], config_dir: Path) -> str | None:
        """The failure detail when neither claude auth env var is set, else ``None``.

        Auth is the ``CLAUDE_CODE_OAUTH_TOKEN`` env var the runner injects from the repo's
        ``env_file`` (an ``ANTHROPIC_API_KEY`` is also sufficient); claude reads it straight from the
        env, so there's no persisted credential file to fall back on — ``config_dir`` is unused.
        """
        if env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
            return None
        return (
            "No auth token — set CLAUDE_CODE_OAUTH_TOKEN in the repo's env_file (see docs/auth.md)"
        )

    def write_credentials(self, config_dir: Path, env: Mapping[str, str]) -> Path | None:
        """No-op: claude authenticates from the env var itself, with no on-disk credential to write."""
        return None

    def resolve_model(self, tier: str) -> str:
        """Map the control plane's abstract model tier to claude's concrete model id (ADR 0014 §3a).

        The control plane stores a CLI-agnostic tier (e.g. ``"primary"``); this is the only place
        that tier becomes a provider's model name (``"primary"`` → ``"opus"``), keeping model
        vocabulary out of ``core``/``workflows``. Unknown values pass through unchanged so a raw
        model id set directly (or a tier already persisted as its resolved name) still reaches
        ``--model`` verbatim, while an unmapped **reserved** tier fails loud instead of leaking
        through unresolved (see :func:`~panopticon.container.cli.base.resolve_tier`).
        """
        return resolve_tier(tier, _MODEL_TIERS)

    def read_hook_payload(self, stdin: TextIO) -> dict[str, Any]:
        """Tolerantly parse the hook's stdin JSON; empty/invalid input yields an empty payload."""
        try:
            raw = stdin.read()
        except (OSError, ValueError):
            return {}
        if not raw or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def has_live_background_task(self, payload: dict[str, Any]) -> bool:
        """Whether the Stop payload reports a still-running background task.

        Reads claude's ``background_tasks`` array (claude ≥ v2.1.145; absent on older builds, where
        this is simply ``False`` and the turn flips as before). An entry is live unless its
        ``status`` is a known terminal one (see :data:`_TERMINAL_STATUSES`).

        Deliberately **type-agnostic**: it ignores each entry's ``type`` so it covers every kind of
        background work that re-wakes the agent — ``shell`` (Bash ``run_in_background``), ``monitor``,
        and background **agents** (``subagent``/``workflow``/``teammate``/``cloud_session``/
        ``mcp_task``) alike. They all re-invoke the agent on completion without a UserPromptSubmit,
        so the turn must stay on the agent for all of them.
        """
        tasks = payload.get("background_tasks")
        if not isinstance(tasks, list):
            return False
        for task in tasks:
            if not isinstance(task, dict):
                return True  # unrecognised shape → assume live (don't hand the turn back early)
            status = task.get("status")
            if not isinstance(status, str) or status.strip().lower() not in _TERMINAL_STATUSES:
                return True
        return False

    def launch_argv(
        self,
        config_dir: Path,
        cwd: Path,
        *,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
    ) -> list[str]:
        """`claude` argv, resuming the project's most recent conversation if one exists.

        The agent runs unattended in a throwaway container on a per-task clone, so it launches with
        ``--dangerously-skip-permissions`` — there's no operator to answer prompts, and the blast
        radius is the task's own checkout. claude keeps per-project transcripts under
        ``<config>/projects/<cwd with '/' → '-'>``; when one is there we ``--continue`` it instead of
        starting fresh. The config dir is a **per-task volume**, so this resumes both within a
        container's life and **across respawn/recreate**. If our path encoding ever misses claude's,
        we simply start fresh — a safe degradation.

        On a **first run** (no prior session) with an ``initial_prompt``, the prompt is appended as a
        positional argument so claude processes it immediately. On a **resumed session**
        (``--continue``) the ``initial_prompt`` is omitted — the agent is already mid-task. When the
        resumed session is the agent's turn (``turn == "agent"``), :data:`INTERRUPT_PROMPT` is
        appended so the agent picks up where it left off rather than waiting for user input.

        ``starting_model`` (a tier, e.g. ``"primary"``) is resolved via :meth:`resolve_model` and passed
        as ``--model`` on the **first run only** — on resume claude uses the conversation's model.
        """
        argv = ["claude", "--dangerously-skip-permissions"]
        overview = config_dir / self.WORKFLOW_OVERVIEW_FILE
        if (
            overview.exists()
        ):  # the whole-workflow map → claude's system prompt (it knows the shape)
            argv += ["--append-system-prompt", overview.read_text()]
        mcp_config = config_dir / self.MCP_CONFIG_FILE
        if mcp_config.exists():  # connect to the task service's MCP server, and *only* it
            argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        project = config_dir / "projects" / str(cwd).replace("/", "-")
        if any(project.glob("*.jsonl")):
            argv.append("--continue")
            if turn == "agent":
                argv.append(INTERRUPT_PROMPT)  # positional: auto-resume after container restart
        else:
            if starting_model:  # first run only — on resume claude uses the conversation's model
                argv += ["--model", self.resolve_model(starting_model)]
            if initial_prompt:
                argv.append(initial_prompt)  # positional: claude's first message
        return argv

    def launch(self, config_dir: Path) -> None:  # pragma: no cover - real LLM; skipif-gated / live
        """Run `claude` (resuming the session if any) in the foreground; return when it exits.

        Unlike an ``exec``, this returns control to the launcher when claude exits, so it can stop
        the container (the task → down → respawn). claude inherits this pane's TTY (the interactive
        surface ``tmux attach`` reaches).
        """
        initial_prompt = os.environ.get("PANOPTICON_INITIAL_PROMPT") or None
        turn = os.environ.get("PANOPTICON_TASK_TURN") or None
        starting_model = os.environ.get("PANOPTICON_STARTING_MODEL") or None
        argv = self.launch_argv(
            config_dir,
            Path.cwd(),
            initial_prompt=initial_prompt,
            turn=turn,
            starting_model=starting_model,
        )
        subprocess.run(argv, env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)})
