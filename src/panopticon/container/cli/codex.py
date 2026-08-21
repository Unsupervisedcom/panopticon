"""The `codex` :class:`~panopticon.container.cli.base.AgentCLI` adapter (ADR 0014, ROADMAP M3.5).

Codex satisfies the same seams as claude against its own surface (ADR 0014 §5 mapping table):

- **config dir** ``~/.codex`` (``CODEX_HOME``), config file ``config.toml``;
- **skills / operations** → custom prompts under ``~/.codex/prompts/<name>.md`` (same
  ``---\\ndescription: …\\n---`` frontmatter claude uses, so the body renderers are shared);
- **MCP** → a ``[mcp_servers.panopticon]`` table in ``config.toml`` over streamable **HTTP** (ADR
  flag 1: codex supports remote HTTP MCP; older builds need ``experimental_use_rmcp_client``);
- **workflow overview** → ``$CODEX_HOME/AGENTS.md`` (our config dir — *never* the repo's
  ``/workspace/AGENTS.md``), which layers additively on top of the repo's own instructions;
- **trust / unattended posture** → ``config.toml`` (project ``trust_level`` + ``approval_policy`` /
  ``sandbox_mode``) so a headless container isn't blocked, on first run *and* on resume;
- **auth** → ``OPENAI_API_KEY``;
- **launch / resume** → ``codex`` first-run vs ``codex resume --last`` (the ``claude --continue``
  analogue), probing ``$CODEX_HOME/sessions`` for a prior transcript.

Scope is **M3.5**: everything needed to boot codex, reach the MCP server, see its skills + overview,
and resume. The **turn-flip hooks** (``write_settings`` wiring, the background-task gating payload)
are **M3.6** — the three hook seam methods are implemented here only enough to keep this class
concrete and degrade safely (see each method's docstring). The determinism invariant holds: this
lives in ``container/`` and only :meth:`launch` execs the real CLI (injected in tests).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, TextIO

from panopticon.container.cli.base import AgentCLI, _Client
from panopticon.container.config import update_toml_config
from panopticon.container.skills import write_commands, write_operation_commands
from panopticon.core.models import Skill

#: The control plane's abstract model **tiers** mapped to codex's concrete model ids (ADR 0014 §3a).
#: The only place a provider model name appears; ``core``/``workflows`` name only the tier. The exact
#: codex model slug is a verify-against-the-pinned-codex item (ROADMAP M3.4 base image); unknown
#: values pass through unchanged (see :meth:`CodexAgentCLI.resolve_model`).
_MODEL_TIERS = {"primary": "gpt-5.6-codex"}


class CodexAgentCLI(AgentCLI):
    """The `codex` adapter (ADR 0014 §5). Config, skills, MCP, overview, trust, auth, launch/resume."""

    name = "codex"
    config_dirname = ".codex"

    #: codex's single config file, under the config dir. MCP, trust, and the unattended posture all
    #: merge into it (each adapter method touches only its own keys, via :func:`update_toml_config`).
    CONFIG_FILE: ClassVar[str] = "config.toml"
    #: Where the skill/operation custom prompts go, relative to the config home.
    PROMPTS_SUBDIR: ClassVar[tuple[str, ...]] = (".codex", "prompts")
    #: The workflow overview file inside the config dir — ``$CODEX_HOME/AGENTS.md`` (ADR 0014 §5).
    WORKFLOW_OVERVIEW_FILE: ClassVar[str] = "AGENTS.md"
    #: Session transcripts live here under the config dir; their presence means "resume" (§ launch).
    SESSIONS_DIRNAME: ClassVar[str] = "sessions"

    def render_skills(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's skills to ``~/.codex/prompts/`` (codex's custom-prompt surface)."""
        skills = [Skill(**s) for s in client.list_skills(task_id)]
        return write_commands(skills, home, task_id, self.PROMPTS_SUBDIR)

    def render_operations(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's declared core operations (advance/drop/…) as codex custom prompts."""
        return write_operation_commands(
            client.list_operations(task_id), home, task_id, self.PROMPTS_SUBDIR
        )

    def write_settings(self, home: Path) -> Path:
        """Return codex's ``config.toml`` path; the turn-flip **hooks are M3.6**, not wired here.

        The launcher calls this to wire the Stop/UserPromptSubmit turn-flip hooks. Codex's hooks
        config schema (and its background-task payload shape) is ADR 0014 flag 2, owned by the
        **Codex turn-flip hooks** slice (M3.6) — until it lands a codex task's turn doesn't auto-flip
        (a documented interim, ADR §5). So this only ensures the config dir exists and returns the
        path other methods merge into; it writes no hook entries. When M3.6 lands, it merges codex's
        ``[hooks]`` block invoking ``python -m panopticon.container.hook`` here.
        """
        config = home / self.config_dirname / self.CONFIG_FILE
        config.parent.mkdir(parents=True, exist_ok=True)
        return config

    def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
        """Point codex at the task service's MCP server via a ``[mcp_servers.panopticon]`` table.

        Codex speaks streamable **HTTP** MCP (ADR 0014 flag 1), so this is the same single control
        plane claude connects to, at ``<service_url>/mcp`` — no auth token (the server is the
        container's own task service). Older codex builds only pick up HTTP MCP with
        ``experimental_use_rmcp_client`` set, so we enable it defensively (a no-op on builds with
        native support). Merged into ``config.toml`` so it coexists with the trust/overview keys.
        """
        config = config_dir / self.CONFIG_FILE
        with update_toml_config(config) as data:
            servers = data.setdefault("mcp_servers", {})
            servers["panopticon"] = {"url": f"{service_url.rstrip('/')}/mcp"}
            data.setdefault("features", {})["experimental_use_rmcp_client"] = True
        return config

    def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
        """Write the whole-workflow map to ``$CODEX_HOME/AGENTS.md`` (``None`` when there's none).

        Codex has no ``--append-system-prompt``; it layers instruction files, reading our config
        dir's ``AGENTS.md`` **on top of** the repo's own ``/workspace/AGENTS.md`` (ADR 0014 §5, flag
        4). We write *only* our config-dir copy — never the working tree's — so the overview reaches
        the agent additively without clobbering the repo's guidance.
        """
        if not overview.strip():
            return None
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / self.WORKFLOW_OVERVIEW_FILE
        path.write_text(overview)
        return path

    def trust_workspace(self, config_dir: Path, cwd: Path) -> Path:
        """Pre-accept codex's trust + approvals so the unattended container runs without prompting.

        A fresh container has no operator to answer codex's first-run gates, so we seed ``config.toml``:

        - ``[projects."<cwd>"] trust_level = "trusted"`` — codex trusts the workspace (and only then
          loads project-scoped config).
        - ``approval_policy = "never"`` + ``sandbox_mode = "danger-full-access"`` — the unattended
          posture. Persisting them in config (not just the launch flag) keeps it unattended **on
          resume** too: codex's ``--dangerously-bypass-approvals-and-sandbox`` is not honored when a
          session is resumed (codex issue #9144), so the config is the durable guarantee. The blast
          radius is the task's own per-task clone.

        Merged (read-modify-write), so it never clobbers the MCP table or anything codex wrote, and
        is idempotent.
        """
        config = config_dir / self.CONFIG_FILE
        with update_toml_config(config) as data:
            data["approval_policy"] = "never"
            data["sandbox_mode"] = "danger-full-access"
            projects = data.setdefault("projects", {})
            projects.setdefault(str(cwd), {})["trust_level"] = "trusted"
        return config

    def auth_missing_detail(self, env: Mapping[str, str]) -> str | None:
        """The failure detail when codex's auth env var is absent, else ``None``.

        Auth is ``OPENAI_API_KEY``, injected by the runner from the repo's ``env_file`` (ADR 0007 /
        0012 generalize per CLI); the launcher wires no credentials.
        """
        if env.get("OPENAI_API_KEY"):
            return None
        return "No auth token — set OPENAI_API_KEY in the repo's env_file (see docs/auth.md)"

    def resolve_model(self, tier: str) -> str:
        """Map the control plane's abstract model tier to codex's concrete model id (ADR 0014 §3a).

        The only place the tier (e.g. ``"primary"``) becomes a provider model name, keeping model
        vocabulary out of ``core``/``workflows``. Unknown values pass through unchanged so a raw model
        id set directly still reaches ``--model`` verbatim.
        """
        return _MODEL_TIERS.get(tier, tier)

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
        """Whether the Stop payload reports still-running background work — **M3.6**, ``False`` for now.

        The turn-flip background-task gating needs codex's background-task payload shape (the
        ``background_tasks`` analogue), which is ADR 0014 flag 2, owned by the Codex turn-flip hooks
        slice (M3.6). Until then this degrades to the plain turn flip — exactly the safe degradation
        claude already uses when the field is absent (an older CLI): the turn flips to the user on
        Stop. Codex's hooks aren't wired yet either (see :meth:`write_settings`), so this isn't
        reached in practice; it's implemented conservatively so it's correct the moment M3.6 wires it.
        """
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
        """`codex` argv, resuming the config dir's most recent session if one exists.

        The agent runs unattended in a throwaway container on a per-task clone, so it launches with
        ``--dangerously-bypass-approvals-and-sandbox`` (the ``claude --dangerously-skip-permissions``
        analogue) — no operator to answer prompts, blast radius the task's own checkout. Codex keeps
        session transcripts under ``$CODEX_HOME/sessions``; when one is present we ``resume --last``
        instead of starting fresh. The config dir is a **per-task volume**, so this resumes both
        within a container's life and **across respawn/recreate**.

        On a **first run** (no prior session) the ``starting_model`` tier is resolved via
        :meth:`resolve_model` and passed as ``--model`` (on resume codex uses the session's model),
        and an ``initial_prompt`` is appended as codex's first message. ``turn`` is accepted for
        signature parity with the claude adapter; auto-continuing a resumed session on the agent's
        turn (claude's interrupt prompt) is deferred with the rest of the turn wiring to M3.6, since
        injecting a prompt into a resumed codex session isn't yet verified.
        """
        argv = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
        sessions = config_dir / self.SESSIONS_DIRNAME
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv += ["resume", "--last"]  # resume the config dir's most recent session
        else:
            if starting_model:  # first run only — on resume codex uses the session's model
                argv += ["--model", self.resolve_model(starting_model)]
            if initial_prompt:
                argv.append(initial_prompt)  # positional: codex's first message
        return argv

    def launch(self, config_dir: Path) -> None:  # pragma: no cover - real LLM; skipif-gated / live
        """Run `codex` (resuming the session if any) in the foreground; return when it exits.

        Like the claude adapter, this returns control to the launcher when codex exits (so it can
        stop the container → the task shows down → respawn). Codex inherits this pane's TTY (the
        interactive surface ``tmux attach`` reaches) and reads its config from ``CODEX_HOME``.
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
        subprocess.run(argv, env={**os.environ, "CODEX_HOME": str(config_dir)})
