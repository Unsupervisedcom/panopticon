"""The `codex` :class:`~panopticon.container.cli.base.AgentCLI` adapter (ADR 0014, ROADMAP M3.5).

Codex satisfies the same seams as claude against its own surface (ADR 0014 §5 mapping table):

- **config dir** ``~/.codex`` (``CODEX_HOME``), config file ``config.toml``;
- **skills / operations** → ``~/.agents/skills/<name>/SKILL.md`` (codex's model-discoverable
  skills mechanism; user scope so nothing reaches the task's working tree; ``---\\nname: …\\n
  description: …\\n---`` frontmatter, body renderers shared with claude);
- **MCP** → a ``[mcp_servers.panopticon]`` table in ``config.toml`` over streamable **HTTP** (ADR
  flag 1: codex supports remote HTTP MCP; older builds need ``experimental_use_rmcp_client``);
- **workflow overview** → ``developer_instructions`` in ``config.toml`` (codex's explicit
  system-prompt injection channel — the ``--append-system-prompt`` analogue; *never* the repo's
  ``/workspace/AGENTS.md``);
- **trust / unattended posture** → ``config.toml`` (project ``trust_level`` + ``approval_policy`` /
  ``sandbox_mode``) so a headless container isn't blocked, on first run *and* on resume;
- **auth** → an API key (``CODEX_API_KEY`` / ``OPENAI_API_KEY``) materialized into
  ``$CODEX_HOME/auth.json`` (a bare env var does *not* log codex in), or a ChatGPT workspace
  access token (``CODEX_ACCESS_TOKEN``) read straight from the env — see :meth:`write_credentials`;
- **launch / resume** → ``codex`` first-run vs ``codex resume <session_id>`` (the ``claude
  --continue`` analogue), selecting the resumable session via :func:`_find_resume_target`.

Scope now includes the **turn-flip hooks** (M3.6): :meth:`~CodexAgentCLI.write_settings` wires
codex's ``[hooks]`` ``Stop`` / ``UserPromptSubmit`` block to the shared callback, and the hook-payload
seam (:meth:`~CodexAgentCLI.read_hook_payload` / :meth:`~CodexAgentCLI.has_live_background_task`)
parses codex's Stop payload. Codex feeds a ``UserPromptSubmit`` hook's stdout back as developer
context (ADR 0014 flag 6), so the briefing + provisioning nudge ride the same channel as claude; its
Stop payload has no background-task array so the flip always hands the turn back (flag 2), and it has
no ``AskUserQuestion`` analogue (flag 7) — both handled as documented. The determinism invariant
holds: this lives in ``container/`` and only :meth:`launch` execs the real CLI (injected in tests).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, TextIO

from panopticon.container.cli.base import AgentCLI, _Client, resolve_tier
from panopticon.container.config import update_toml_config
from panopticon.container.hooks import HOOK_COMMAND
from panopticon.container.skills import write_agent_operation_skills, write_agent_skills
from panopticon.core.models import Skill


def _find_resume_target(sessions_dir: Path) -> str | None:
    """Return the session id of the newest resumable codex session, or ``None``.

    ``$CODEX_HOME/sessions`` is shared by **all** codex invocations in the container —
    ``codex exec`` subprocesses (anything the agent shells out to) and codex-tui's own
    internal subagent threads (e.g. compaction) all write ``.jsonl`` rollout files there.
    Resuming by ``--last`` (newest mtime) can therefore land on a non-resumable or wrong
    session. This function reads only the **first line** of each file (the ``session_meta``
    record, cheap regardless of session length) and filters to sessions where:

    - ``payload["originator"] == "codex-tui"`` — interactive TUI, not ``codex_exec``
    - ``payload["thread_source"] == "user"`` — root thread, not an internal subagent thread

    Returns ``payload["id"]`` of the eligible file with the highest ``st_mtime_ns`` (integer
    nanoseconds — float mtime loses sub-second precision). Malformed or empty first lines and
    any ``OSError`` are silently skipped. Returns ``None`` when nothing qualifies.
    """
    best_mtime: int = -1
    best_id: str | None = None

    for path in sessions_dir.rglob("*.jsonl"):
        try:
            first_line = path.read_text().split("\n", 1)[0].strip()
            if not first_line:
                continue
            record = json.loads(first_line)
            if not isinstance(record, dict):
                continue
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if payload.get("originator") != "codex-tui":
                continue
            if payload.get("thread_source") != "user":
                continue
            session_id = payload.get("id")
            if not session_id or not isinstance(session_id, str):
                continue
            mtime = path.stat().st_mtime_ns
            if mtime > best_mtime:
                best_mtime = mtime
                best_id = session_id
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    return best_id


#: The control plane's abstract model **tiers** mapped to codex's concrete model ids (ADR 0014 §3a).
#: The only place a provider model name appears; ``core``/``workflows`` name only the tier. ``primary``
#: maps to codex's flagship (``gpt-5.6-sol``), verified against the pinned codex release (the
#: ``CODEX_VERSION`` build arg in ``docker/Dockerfile``); unknown values pass through unchanged (see
#: :meth:`CodexAgentCLI.resolve_model`).
_MODEL_TIERS = {"primary": "gpt-5.6-sol"}


def _command_hook(actor: str, event: str) -> dict[str, Any]:
    """One codex hook group: run the shared turn-flip callback with ``<actor> <event>``.

    Codex nests a command under an event as ``{"hooks": [{"type": "command", "command": …}]}`` — the
    ``[[hooks.<Event>]]`` → ``[[hooks.<Event>.hooks]]`` TOML shape. The command is the CLI-agnostic
    callback (:data:`~panopticon.container.hooks.HOOK_COMMAND`) claude invokes too.
    """
    return {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} {actor} {event}"}]}


class CodexAgentCLI(AgentCLI):
    """The `codex` adapter (ADR 0014 §5). Config, skills, MCP, overview, trust, auth, launch/resume."""

    name = "codex"
    config_dirname = ".codex"

    #: codex's single config file, under the config dir. MCP, trust, and the unattended posture all
    #: merge into it (each adapter method touches only its own keys, via :func:`update_toml_config`).
    CONFIG_FILE: ClassVar[str] = "config.toml"
    #: Session transcripts live here under the config dir; their presence means "resume" (§ launch).
    SESSIONS_DIRNAME: ClassVar[str] = "sessions"
    #: codex's credentials file under the config home — what ``codex login --with-api-key`` writes.
    AUTH_FILE: ClassVar[str] = "auth.json"
    #: Env-var spellings carrying an OpenAI API key we materialize into :attr:`AUTH_FILE`.
    API_KEY_VARS: ClassVar[tuple[str, ...]] = ("CODEX_API_KEY", "OPENAI_API_KEY")
    #: The ChatGPT workspace access token (the ``claude setup-token`` analog); read from the env, no file.
    ACCESS_TOKEN_VAR: ClassVar[str] = "CODEX_ACCESS_TOKEN"

    def render_skills(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's skills to ``~/.agents/skills/`` (codex's model-discoverable surface)."""
        skills = [Skill(**s) for s in client.list_skills(task_id)]
        return write_agent_skills(skills, home, task_id)

    def render_operations(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's declared core operations (advance/drop/…) as codex agent skills."""
        return write_agent_operation_skills(client.list_operations(task_id), home, task_id)

    def write_settings(self, home: Path) -> Path:
        """Wire codex's turn-flip hooks into ``config.toml``; return the path (ADR 0014 §5, M3.6).

        Codex's hooks live under a ``[hooks]`` table keyed by event, each event an array of groups
        whose ``hooks`` array holds ``{type = "command", command = …}`` entries (the same shape
        claude uses, just TOML). We wire the two turn-flip events the same callback
        (:mod:`panopticon.container.hook`) serves for claude:

        - **Stop** → ``hook user stop`` (flip the ball to the user; the callback applies the
          background-task guard). The callback prints nothing on the stop path, satisfying codex's
          rule that plain-text stdout is invalid for ``Stop`` (JSON-only).
        - **UserPromptSubmit** → ``hook agent prompt`` (flip to the agent, then print the phase
          briefing + provisioning nudge — codex feeds a ``UserPromptSubmit`` hook's stdout back as
          developer context, so the same channel claude relies on works here; ADR 0014 flag 6).

        Codex's ``Stop``/``UserPromptSubmit`` don't support a ``matcher``, and codex has no
        ``AskUserQuestion`` tool, so — unlike claude — we wire *no* ``PreToolUse``/``PostToolUse``
        pair; the "agent is asking the user" turn state simply stays on the agent until the next Stop
        (the documented degradation, ADR 0014 flag 7). Merged read-modify-write so it coexists with
        the MCP / trust / overview keys already in ``config.toml``.
        """
        config = home / self.config_dirname / self.CONFIG_FILE
        with update_toml_config(config) as data:
            hooks = data.setdefault("hooks", {})
            hooks["Stop"] = [_command_hook("user", "stop")]
            hooks["UserPromptSubmit"] = [_command_hook("agent", "prompt")]
        return config

    def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
        """Point codex at the task service's MCP server via a ``[mcp_servers.panopticon]`` table.

        Codex speaks streamable **HTTP** MCP (ADR 0014 flag 1), so this is the same single control
        plane claude connects to, at ``<service_url>/mcp`` — no auth token (the server is the
        container's own task service). Older codex builds only pick up HTTP MCP with
        ``experimental_use_rmcp_client`` set, so we enable it defensively (a no-op on builds with
        native support). ``features.apps = false`` disables codex's built-in apps connector, which
        cannot start in the container and otherwise stalls every spawn on its 30 s MCP timeout
        (the ``[mcp_servers.codex_apps] enabled = false`` alternative is invalid config that
        crash-loops codex — the feature flag is the only safe disable). Merged into ``config.toml``
        so it coexists with the trust/overview keys.
        """
        config = config_dir / self.CONFIG_FILE
        with update_toml_config(config) as data:
            servers = data.setdefault("mcp_servers", {})
            servers["panopticon"] = {"url": f"{service_url.rstrip('/')}/mcp"}
            features = data.setdefault("features", {})
            features["experimental_use_rmcp_client"] = True
            features["apps"] = False
        return config

    def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
        """Deliver the whole-workflow map via ``developer_instructions`` in ``config.toml``.

        ``developer_instructions`` is codex's explicit system-prompt injection channel — the
        ``--append-system-prompt`` analogue (ADR 0014 §5, flag 4). Writing it to ``config.toml``
        (rather than relying on ``$CODEX_HOME/AGENTS.md`` layering) gives a stronger, unambiguous
        delivery: the content reaches the agent directly without depending on codex's file-layering
        semantics. We never touch the working tree's ``/workspace/AGENTS.md``. Returns the
        ``config.toml`` path, or ``None`` when there's no overview to deliver.
        """
        if not overview.strip():
            return None
        config = config_dir / self.CONFIG_FILE
        with update_toml_config(config) as data:
            data["developer_instructions"] = overview
        return config

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

    def auth_missing_detail(self, env: Mapping[str, str], config_dir: Path) -> str | None:
        """The failure detail when codex has no way to authenticate, else ``None``.

        Codex is satisfied by any of the auth vars the runner injects from the repo's ``env_file``
        (ADR 0007 / 0012 generalize per CLI) — an API key (``CODEX_API_KEY`` / ``OPENAI_API_KEY``,
        which :meth:`write_credentials` materializes into ``auth.json``) or a ChatGPT workspace access
        token (``CODEX_ACCESS_TOKEN``, read straight from the env) — **or** a pre-existing
        ``auth.json`` on the per-task config volume (a container already logged in, e.g. carried
        across respawn — which a bare env check would wrongly fail). Presence checks only: we don't
        validate the key shape (OpenAI's format isn't ours to pin); an invalid credential surfaces at
        codex's first call.
        """
        if any(env.get(var) for var in (*self.API_KEY_VARS, self.ACCESS_TOKEN_VAR)):
            return None
        if (config_dir / self.AUTH_FILE).exists():
            return None
        return (
            "No codex auth — set OPENAI_API_KEY (or CODEX_API_KEY / CODEX_ACCESS_TOKEN) in the "
            "repo's env_file (see docs/auth.md)"
        )

    def write_credentials(self, config_dir: Path, env: Mapping[str, str]) -> Path | None:
        """Materialize codex's ``auth.json`` from an API key in the env, and pin the file cred store.

        A bare ``OPENAI_API_KEY`` in the container env does **not** log codex in — codex
        authenticates from ``$CODEX_HOME/auth.json`` and may otherwise reach for an OS keyring the
        container lacks. So we:

        - set ``cli_auth_credentials_store = "file"`` (top-level ``config.toml``) so codex reads
          credentials from the file, never a keyring — done unconditionally, so it also governs a
          pre-existing ``auth.json`` carried across respawn;
        - when ``auth.json`` is absent, render it from ``CODEX_API_KEY`` or ``OPENAI_API_KEY`` in the
          exact shape ``codex login --with-api-key`` writes — ``{"auth_mode": "apikey",
          "OPENAI_API_KEY": <key>}`` — at mode ``0600``.

        **Idempotent: an existing ``auth.json`` is never clobbered**, so a container already logged in
        keeps its credentials. Returns the ``auth.json`` path when written, else ``None`` (no API key,
        or one already present). A workspace access token (``CODEX_ACCESS_TOKEN``) needs no file —
        codex reads it from the env — so it doesn't trigger a write here (the auth gate accepts it).
        """
        config = config_dir / self.CONFIG_FILE
        with update_toml_config(config) as data:
            data["cli_auth_credentials_store"] = "file"
        auth = config_dir / self.AUTH_FILE
        if auth.exists():
            return None
        key = next((env[var] for var in self.API_KEY_VARS if env.get(var)), None)
        if not key:
            return None
        config_dir.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key}))
        auth.chmod(0o600)
        return auth

    def resolve_model(self, tier: str) -> str:
        """Map the control plane's abstract model tier to codex's concrete model id (ADR 0014 §3a).

        The only place the tier (e.g. ``"primary"``) becomes a provider model name, keeping model
        vocabulary out of ``core``/``workflows``. Unknown values pass through unchanged so a raw model
        id set directly still reaches ``--model`` verbatim, while an unmapped **reserved** tier fails
        loud instead of leaking through unresolved (see
        :func:`~panopticon.container.cli.base.resolve_tier`).
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
        """Whether the Stop payload reports still-running background work (gates the turn flip).

        Always ``False`` for codex: its documented ``Stop`` payload (ADR 0014 flag 2, verified against
        the hooks schema) is ``session_id`` / ``transcript_path`` / ``cwd`` / ``hook_event_name`` /
        ``model`` / ``permission_mode`` / ``turn_id`` / ``stop_hook_active`` / ``last_assistant_message``
        — it carries **no** background-task array (unlike claude's ``background_tasks``), and codex's
        ``Stop`` fires only when the turn has genuinely ended, so there's nothing in flight to strand.
        The turn flips to the user, matching claude's exact behaviour when the field is absent.
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
        """`codex` argv, resuming the most recent resumable session by id if one exists.

        The agent runs unattended in a throwaway container on a per-task clone, so it launches with
        ``--dangerously-bypass-approvals-and-sandbox`` (the ``claude --dangerously-skip-permissions``
        analogue) — no operator to answer prompts, blast radius the task's own checkout. Codex keeps
        session transcripts under ``$CODEX_HOME/sessions``; :func:`_find_resume_target` scans them
        and returns the id of the newest session whose first-line ``session_meta`` record marks it as
        a resumable interactive TUI root thread (``originator=codex-tui``, ``thread_source=user``).
        When one is found, ``codex resume <session_id>`` is used instead of starting fresh. The
        config dir is a **per-task volume**, so this resumes both within a container's life and
        **across respawn/recreate**.

        ``--dangerously-bypass-hook-trust`` bypasses codex's per-hash interactive trust prompt for
        unrecognised hooks (our Stop/UserPromptSubmit hooks, wired in :meth:`write_settings`). With
        ``session_id`` now a positional argument to ``resume``, all bypass flags are placed at the
        global level (before the subcommand) so they parse correctly in both first-run and resume
        paths. ``--no-alt-screen`` renders codex output into the tmux scrollback (not the alternate
        screen) so ``tmux attach`` history stays useful. On **resume** with ``turn == "agent"`` (the
        agent was interrupted mid-turn), the interrupt prompt ``"You were interrupted. Continue."``
        is appended as codex's first positional message so the agent picks up where it left off. On a
        **first run** (no resumable session) the ``starting_model`` tier is resolved via
        :meth:`resolve_model` and passed as ``--model`` (on resume codex uses the session's model),
        and ``initial_prompt`` is appended as the first message. ``starting_model`` may carry a
        ``<tier>:<effort>`` suffix (e.g. ``"primary:high"``) — the suffix is split off and passed
        as ``--config model_reasoning_effort=<effort>`` (codex takes effort as a config key, not a
        flag).
        """
        argv = [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--no-alt-screen",
        ]
        sessions_dir = config_dir / self.SESSIONS_DIRNAME
        session_id = _find_resume_target(sessions_dir) if sessions_dir.exists() else None
        if session_id:
            argv += ["resume", session_id]
            if turn == "agent":
                argv.append("You were interrupted. Continue.")
        else:
            if starting_model:  # first run only — on resume codex uses the session's model
                tier, _, effort = starting_model.partition(":")
                argv += ["--model", self.resolve_model(tier)]
                if effort:
                    argv += ["--config", f"model_reasoning_effort={effort}"]
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
