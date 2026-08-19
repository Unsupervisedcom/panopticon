# 0014 — The agent-CLI adapter seam (Milestone 3 kickoff)

- Status: Accepted
- Date: 2026-08-19
- Deciders: Charlie Scherer

## Context

Panopticon runs one agent CLI: `claude`. GOALS.md states the system must "not [be] locked to
Claude … [it] must accommodate other agent CLIs (see Milestone 3)", and the ROADMAP's Milestone 3
is "agent-runner adapters beyond `claude` + base-image variants (ADR 0005)". Codex is the first
additional target.

The `claude` dependency is concentrated in the **container layer** — the only LLM-bearing package
(the determinism invariant, AGENTS.md). Six modules bake in claude-specific decisions:

- `container/agent.py` — the launcher. Hard-codes the `.claude` config dir, `.claude.json` trust
  file, the MCP-config JSON shape (`{"mcpServers": …}` pointed at via `--mcp-config
  --strict-mcp-config`), the workflow-overview delivery (`--append-system-prompt`), the launch argv
  (`claude --dangerously-skip-permissions`, `--continue`, `--model`, positional first prompt), the
  session-resume probe (`projects/<cwd>/…*.jsonl`), the trust pre-accept
  (`hasCompletedOnboarding` / `hasTrustDialogAccepted` / `hasAcknowledgedCostThreshold`), and the
  auth check (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`).
- `container/skills.py` — renders skills + operations to `.claude/commands/<name>.md` with claude
  frontmatter.
- `container/hooks.py` — renders `.claude/settings.json` turn-flip hooks (`Stop`,
  `UserPromptSubmit`, `PreToolUse`/`PostToolUse` on `AskUserQuestion`) plus
  `skipDangerousModePermissionPrompt`.
- `container/hook.py` — the hook **callback**; parses claude's payload (`transcript_path`,
  `background_tasks` array ≥ v2.1.145) to decide the turn flip and record tokens.
- `container/config.py` — the read-merge-write for claude's JSON config files.
- `container/pricing.py` — Anthropic-specific cost weights (per-tier ratios) for the token report
  and the planning estimate.

Milestone 3 needs a second CLI to drop in **without touching the control plane** and without a
second copy of the launcher's control flow. This ADR settles the seam. It is **design only** — no
code lands here; it is the contract the M3 implementation slices build against.

The comments already scattered through those modules ("M3 revisits for other CLIs",
"claude-specific renderer", the `TODO(non-claude-agents)` in `pricing.py`) are the informal
version of this decision; this ADR makes it the plan of record.

## Decision

### 1. An `AgentCLI` adapter interface in `container/`

Introduce an `AgentCLI` abstraction (an ABC in `container/agent_cli.py`; the concrete name is
settled here, the method signatures are refined during the refactor slice) that captures **every**
claude-specific decision as one seam each. The launcher (`agent.py`) becomes CLI-agnostic: it
orchestrates the bootstrap-then-launch sequence against the adapter, holding no `claude` literal.

The seams, one method (or small method group) each:

| Seam | claude today | codex target |
| --- | --- | --- |
| **config dir** | `~/.claude` (`CLAUDE_CONFIG_DIR`) | `~/.codex` (`CODEX_HOME`) |
| **render skills** | `.claude/commands/<name>.md` + frontmatter | `~/.codex/prompts/<name>.md` |
| **render core-operation commands** (advance/drop/…) | same `.claude/commands/` renderer | same prompts dir |
| **render turn-flip hooks + callback wiring** | `.claude/settings.json` `hooks` block | codex hooks in `config.toml` |
| **parse hook payload** (turn flip + token/bg-task detection) | `background_tasks`, `transcript_path` JSON on stdin | codex hook payload schema (verify) |
| **write MCP client config** | `panopticon-mcp.json` (`{"mcpServers": …}`) via `--mcp-config` | `[mcp_servers.panopticon]` in `config.toml` |
| **render workflow-overview / system prompt** | `--append-system-prompt <overview>` | `AGENTS.md` (or codex system-prompt flag) |
| **build launch argv incl. resume** | `claude --dangerously-skip-permissions [--continue \| --model M PROMPT]` | `codex …` first-run vs `codex resume --last`/session-id |
| **trust / first-run pre-accept** | `.claude.json` onboarding + trust + cost keys | codex trust / sandbox-approval seed |
| **auth env var(s) + missing-auth check** | `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` / codex login token |
| **cost weights** | Anthropic per-tier ratios (`pricing.py`) | OpenAI/codex per-tier weights |

The bootstrap/launch split (AGENTS.md "No LLMs in tests") is preserved: the adapter's rendering
methods are **deterministic and unit-tested with fakes**; only the final `launch(config_dir)`
execs the real CLI and is injected in tests.

`config.py`'s read-merge-write stays a shared helper — both claude (JSON) and any other
JSON-configured CLI use it; a TOML-configured CLI (codex) gets an analogous TOML merge helper.

### 2. An adapter registry keyed by CLI name

A registry maps a CLI name (`"claude"`, `"codex"`) to its `AgentCLI` implementation. The launcher
resolves its adapter from the CLI name the runner passes in via an env var
(`PANOPTICON_AGENT_CLI`, defaulting to `"claude"` when unset — so existing containers are
unchanged). Adding a CLI is: implement the ABC, register it under its name. No launcher edit, no
control-plane edit — the same drop-in shape as workflow discovery (ADR 0004).

### 3. CLI selection model: task → repo → default

CLI choice is a first-class, recorded fact, resolved like secrets and image layers:

- `Repo.agent_cli: str` — the repo's default CLI (defaults to `"claude"`).
- `Task.agent_cli: str | None` — a nullable per-task override.
- **Resolution order:** `Task.agent_cli` → `Repo.agent_cli` → `"claude"`. Resolved **host-side**
  by the session service (where the container is spawned), like every other spawn input, and passed
  into the container as `PANOPTICON_AGENT_CLI`.

The resolved CLI drives three host-side choices at spawn: the **base-image variant** (§4), the
**auth env var** the runner injects from the repo's `env_file` (the env-file already carries
whatever the CLI needs — `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`; the ADR 0007 secret model
already generalizes, so no new mechanism), and the `PANOPTICON_AGENT_CLI` env var the launcher
reads. This is a schema change (`Repo`/`Task` rows + an Alembic migration) owned by the
**selection-seam slice**, not this ADR.

### 4. Base-image variants (ADR 0005 base tier)

Today there is one base image, `panopticon-base` (`DEFAULT_IMAGE`), on which `images.py` composes
`base → workflow → repo`. Each CLI needs its own runtime installed (the `claude` CLI vs the
`codex` CLI), so the **base tier becomes per-CLI**:

- `panopticon-base-claude`, `panopticon-base-codex` — one base Dockerfile variant per CLI, each
  installing that CLI on the shared base runtime.
- `images.py` selects the base variant from the resolved CLI name (`base_image(cli)` →
  `panopticon-base-<cli>`), then composes the workflow + repo layers onto it exactly as now. The
  composed tag gains the CLI dimension so a repo built for two CLIs doesn't collide.

The workflow and repo layers are CLI-agnostic and unchanged. `DEFAULT_IMAGE` becomes
`panopticon-base-claude` (preserving today's behavior when nothing selects a CLI).

### 5. The Codex mapping — concrete, with flags to verify

Codex satisfies every seam, using these current facts:

- **Config dir:** `~/.codex`, overridable via `CODEX_HOME`. Config file `config.toml`.
- **Skills / prompts:** custom prompts live under `~/.codex/prompts/<name>.md` — the skill and
  operation renderers target that dir instead of `.claude/commands/`.
- **MCP:** codex has `[mcp_servers.<name>]` tables in `config.toml` — the panopticon server is one
  entry pointed at `<service_url>/mcp`.
- **Hooks:** codex has a hooks system with `SessionStart` / `UserPromptSubmit` / `Stop` /
  `PreToolUse` / `PostToolUse` events, configured in `config.toml` — the same turn-flip contract
  (Slice 4) maps across, invoking `python -m panopticon.container.hook`.
- **System prompt:** the workflow overview goes in `AGENTS.md` (codex reads it) rather than an
  `--append-system-prompt` flag.
- **Resume:** `codex resume --last` (or a session id) is the `--continue` analogue; the launcher's
  first-run-vs-resume decision keeps its shape, only the argv changes.
- **Auth:** `OPENAI_API_KEY` (or the codex login token) instead of `CLAUDE_CODE_OAUTH_TOKEN`.

**Flags to verify during the codex-adapter slices** (not blocking this ADR; each is a small
spike the implementer resolves against the installed codex version):

1. MCP transport — HTTP vs stdio support (we serve HTTP today; if codex is stdio-only we front it
   with a local proxy or add an stdio MCP entrypoint).
2. The exact codex **hooks config schema** and payload shape (for the turn-flip callback's
   background-task/token parsing — the `background_tasks` analogue).
3. The `CODEX_HOME` config-dir override and its precedence.
4. The unattended / **skip-approvals sandbox** flag (the `--dangerously-skip-permissions`
   analogue — codex runs headless in a throwaway container on a per-task clone).

### 6. The determinism invariant holds

Adapters live **only** in `container/`, the sole LLM-bearing package. The seam adds **no** LLM
calls to the control plane, and the deterministic turn mechanism (the task service's `set_turn` /
responsibilities / state machine) is unchanged — only the CLI-specific **hook wiring and payload
parsing** are per-adapter. The task service never learns which CLI a container runs; it sees the
same REST/MCP surface regardless.

## Consequences

- **Enables M3.** A reviewer can implement the refactor and the Codex adapter from this ADR without
  further design decisions. The seam is the stable contract every later M3 slice builds against.
- **Small blast radius.** Only `container/` and the host-side spawn/image selection change; `core`,
  `taskservice`, `workflows`, and the dashboard are untouched (bar the `Repo`/`Task.agent_cli`
  columns, an additive migration).
- **The planning-token prompts generalize.** `pricing.py`'s weights becoming per-CLI resolves the
  standing `TODO(non-claude-agents)`; the planning prompts that cite Anthropic ratios
  (`PlannedWorkflow.TOKEN_ESTIMATED`, `orchestrator._SPAWN_TASK_INSTRUCTIONS`) become
  backend-aware in the same slice.
- **Cost:** one base image per CLI to build and keep current (the `Makefile`/`make build` grows a
  per-CLI target), and a second CLI's quirks (auth, sandbox, resume semantics) to track.

## References

- GOALS.md — "Not locked to Claude … accommodate other agent CLIs (Milestone 3)".
- ROADMAP.md — Milestone 3 slices (decomposed alongside this ADR).
- ADR 0004 — workflow abstraction + the skill/operation command surface this renders per CLI.
- ADR 0005 — composable images; the base tier this makes per-CLI.
- ADR 0007 — the secret model that already generalizes to per-CLI auth env vars.
- ADR 0012 — retired the OAuth creds volume; auth is now the `CLAUDE_CODE_OAUTH_TOKEN` env var this
  generalizes per CLI.
