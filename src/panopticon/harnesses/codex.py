"""The codex harness — OpenAI's ``codex`` CLI as an alternate agent runtime (Milestone 3).

Everything is rendered into ``$CODEX_HOME`` (``<home>/.codex``, the per-task config volume) and
``<home>/.agents/skills`` on every container start:

- ``config.toml`` — the task service's MCP server (streamable HTTP), the workflow overview as
  ``developer_instructions`` (codex's analog of claude's ``--append-system-prompt``), workspace
  trust, file-backed credentials, and the **turn-flip hooks**: codex ships a Claude-Code-
  compatible hooks system (``Stop`` / ``UserPromptSubmit``, same JSON-on-stdin shape), so both
  events invoke the same :mod:`panopticon.container.hook` callback claude uses. Hook side
  effects degrade safely where codex's payload differs (the Stop token report and the prompt
  briefing are best-effort by design).
- Skills and operations — codex reads skills from ``~/.agents/skills/<name>/SKILL.md``
  (its custom-prompts mechanism is deprecated). Rendered user-scope, not into the workspace,
  so the task's clone stays clean.
- ``auth.json`` — codex's credential file. Three ways in, checked in order: a **mounted
  credential dir** (the repo's ``credential_dir`` — a ChatGPT-subscription ``auth.json`` shared
  across the repo's containers; codex re-reads the file before refreshing and writes through
  the symlink, so concurrent sessions converge on it), a **``CODEX_API_KEY``/``OPENAI_API_KEY``**
  env-file var (rendered into an api-key ``auth.json``), or a **``CODEX_ACCESS_TOKEN``**
  (ChatGPT Business/Enterprise workspace token, read by codex straight from the env).

Facts pinned against codex-cli 0.144.4 (config schema + observed behavior); see docs/auth.md.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from panopticon.harnesses.base import (
    INTERRUPT_PROMPT,
    BootstrapContext,
    Harness,
    LaunchContext,
)
from panopticon.harnesses.claude import HOOK_COMMAND, _task_id_note

#: The codex release the harness image layer installs — the version the config rendering and
#: auth behavior (symlink write-through, reload-before-refresh) were verified against.
CODEX_VERSION = "0.144.4"

#: The credential file codex expects under ``$CODEX_HOME``.
AUTH_FILE = "auth.json"


def _toml_str(value: str) -> str:
    """``value`` as a TOML basic string. JSON string escaping is valid TOML basic-string
    escaping (``\\"``, ``\\\\``, ``\\n``, ``\\uXXXX``), so ``json.dumps`` is the encoder."""
    return json.dumps(value)


def render_config(service_url: str, overview: str, cwd: Path) -> str:
    """The ``config.toml`` panopticon owns for a task's codex (regenerated each start).

    Top-level keys first (TOML requires it), then the tables: the panopticon MCP server
    (streamable HTTP at ``<service_url>/mcp``), workspace trust for ``cwd`` (codex's analog of
    claude's trust dialog — unattended containers can't answer it), and the turn-flip hooks
    (the same contract, command, and actor/event arguments as the claude settings)."""
    lines: list[str] = []
    if overview.strip():
        # The whole-workflow map as a `developer`-role message — codex's system-prompt seam.
        lines.append(f"developer_instructions = {_toml_str(overview)}")
    # Credentials stay a plain file (never the OS keyring): containers have no keyring, and the
    # subscription flow shares auth.json across sessions via the credential-dir mount.
    lines.append('cli_auth_credentials_store = "file"')
    lines += [
        "",
        "[mcp_servers.panopticon]",
        f"url = {_toml_str(service_url.rstrip('/') + '/mcp')}",
        "",
        # codex's built-in `codex_apps` connector can't start in the container and would stall
        # every spawn on its 30s startup timeout — disable it; the panopticon server is the
        # only MCP surface a task needs.
        "[mcp_servers.codex_apps]",
        "enabled = false",
        "",
        f"[projects.{_toml_str(str(cwd))}]",
        'trust_level = "trusted"',
    ]
    for event, actor, side_effect in (
        ("Stop", "user", "stop"),
        ("UserPromptSubmit", "agent", "prompt"),
    ):
        lines += [
            "",
            f"[[hooks.{event}]]",
            f"[[hooks.{event}.hooks]]",
            'type = "command"',
            f"command = {_toml_str(f'{HOOK_COMMAND} {actor} {side_effect}')}",
        ]
    return "\n".join(lines) + "\n"


def render_skill(name: str, description: str, instructions: str, task_id: str) -> str:
    """The ``SKILL.md`` body for one skill/operation: codex's frontmatter + the procedure."""
    return f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n{_task_id_note(task_id)}"


def write_skills(skills: Mapping[str, tuple[str, str]], root: Path, task_id: str) -> list[Path]:
    """Write ``name → (description, instructions)`` to ``<root>/.agents/skills/<name>/SKILL.md``.

    User-scope (codex also reads repo-scope ``.agents/skills`` — deliberately unused so the
    task's clone stays clean; nothing panopticon renders should end up in a commit)."""
    written = []
    for name, (description, instructions) in skills.items():
        skill_dir = root / ".agents" / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(render_skill(name, description, instructions, task_id))
        written.append(path)
    return written


def operation_instructions(name: str, target_state: str, task_id: str) -> str:
    """The procedure body for a core operation (advance/drop/…) — same contract as claude's."""
    return (
        f"Apply this workflow's `{name}` operation — it moves the task to **{target_state}**. "
        f'Invoke it with the `apply_operation` tool (`operation="{name}"`, `task_id="{task_id}"`); '
        f"don't edit the state directly. It's gated on the current state's responsibilities and "
        f"starts a new turn."
    )


class CodexHarness(Harness):
    """OpenAI's ``codex`` CLI behind the harness interface."""

    name: ClassVar[str] = "codex"
    config_dirname: ClassVar[str] = ".codex"

    def image_layer(self) -> str:
        """Install the pinned codex release: the statically-linked musl binary from GitHub
        releases (no runtime deps — the base image needs no node). Runs as root (the base image's
        build user); the binary is world-executable."""
        return (
            "RUN set -eux; \\\n"
            '    arch="$(uname -m)"; \\\n'
            '    case "$arch" in \\\n'
            '      x86_64) triple="x86_64-unknown-linux-musl" ;; \\\n'
            '      aarch64) triple="aarch64-unknown-linux-musl" ;; \\\n'
            '      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\\n'
            "    esac; \\\n"
            "    curl --fail --silent --show-error --location \\\n"
            f'      "https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/codex-$triple.tar.gz" \\\n'
            "      | tar --extract --gzip --directory /usr/local/bin; \\\n"
            '    if [ -e "/usr/local/bin/codex-$triple" ]; then mv "/usr/local/bin/codex-$triple" /usr/local/bin/codex; fi; \\\n'
            "    chmod 0755 /usr/local/bin/codex"
        )

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        if (
            environ.get("CODEX_API_KEY")
            or environ.get("OPENAI_API_KEY")
            or environ.get("CODEX_ACCESS_TOKEN")
        ):
            return None
        if (self.config_dir(home) / AUTH_FILE).exists():  # e.g. persisted on the config volume
            return None
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            return None
        return (
            "No codex credentials — set CODEX_API_KEY (or CODEX_ACCESS_TOKEN) in the repo's "
            "env_file, or give the repo a credential_dir holding a ChatGPT auth.json "
            "(see docs/auth.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            render_config(ctx.service_url, ctx.overview, ctx.cwd)
        )
        entries: dict[str, tuple[str, str]] = {
            s.name: (s.description, s.instructions) for s in ctx.skills
        }
        for name, target_state in ctx.operations.items():
            entries[name] = (
                f"Apply the workflow's '{name}' operation.",
                operation_instructions(name, target_state, ctx.task_id),
            )
        write_skills(entries, ctx.home, ctx.task_id)
        self._ensure_auth(config_dir, ctx.environ)

    def _ensure_auth(self, config_dir: Path, environ: Mapping[str, str]) -> None:
        """Materialize ``auth.json`` when absent — from the credential-dir mount (subscription;
        a symlink, so refreshes converge on the shared file) or an env-file API key (rendered in
        the shape ``codex login --with-api-key`` writes). ``CODEX_ACCESS_TOKEN`` needs no file —
        codex reads it from the environment. Idempotent; never clobbers an existing auth.json."""
        auth = config_dir / AUTH_FILE
        if auth.exists() or auth.is_symlink():
            return
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            auth.symlink_to(Path(credentials) / AUTH_FILE)
            return
        if key := (environ.get("CODEX_API_KEY") or environ.get("OPENAI_API_KEY")):
            auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key}, indent=2))
            os.chmod(auth, 0o600)

    def argv(self, ctx: LaunchContext) -> list[str]:
        """``codex`` argv — resume the config volume's most recent session when one exists.

        The container is the sandbox (same posture as ``claude --dangerously-skip-permissions``):
        ``--dangerously-bypass-approvals-and-sandbox`` because there is no operator to approve and
        codex's own Linux sandbox (bubblewrap) needs unprivileged user namespaces Docker doesn't
        grant; ``--dangerously-bypass-hook-trust`` because the panopticon-rendered hooks would
        otherwise stop on an interactive per-hash trust prompt. Sessions live under
        ``$CODEX_HOME/sessions`` — any recorded one means ``resume --last`` (the per-task
        ``CODEX_HOME`` guarantees "most recent" is this task's); like claude, a resume on the
        agent's turn gets :data:`INTERRUPT_PROMPT` so it picks up where it left off."""
        bypass = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
        sessions = self.config_dir(ctx.home) / "sessions"
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv = ["codex", "resume", "--last", *bypass]
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)
            return argv
        argv = ["codex", *bypass]
        if ctx.starting_model:  # first run only — a resume keeps the session's model
            argv += ["--model", ctx.starting_model]
        if ctx.initial_prompt:
            argv.append(ctx.initial_prompt)  # positional: the agent's first message
        return argv

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        return {"CODEX_HOME": str(self.config_dir(ctx.home))}
