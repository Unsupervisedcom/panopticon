# 0014 — The agent-CLI adapter seam (Milestone 3 kickoff)

- Status: Accepted
- Date: 2026-08-19
- Deciders: Charlie Scherer

## Context

Panopticon runs one agent CLI: `claude`. GOALS.md states the system must "not [be] locked to
Claude … [it] must accommodate other agent CLIs (see Milestone 3)", and the ROADMAP's Milestone 3
is "agent-runner adapters beyond `claude` + base-image variants (ADR 0005)". Codex is the first
additional target.

Most of the `claude` dependency sits in the **container layer** — the only LLM-bearing package
(the determinism invariant, AGENTS.md). Five modules bake in claude-specific decisions:

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
- `container/hook.py` — the hook **callback**; parses claude's payload (`background_tasks` array
  ≥ v2.1.145) to gate the turn flip, and delivers the phase briefing + provisioning nudge via the
  hook's stdout.
- `container/config.py` — the read-merge-write for claude's JSON config files.

One claude-specific fact also **leaks into the control plane** today, and the ADR must name a seam
for it rather than pretend the dependency is container-only:

- **Model naming.** `Workflow.default_model = "opus"` (`core/workflow.py:106`) seeds
  `Task.starting_model`, which the spawner injects as `PANOPTICON_STARTING_MODEL` and the launcher
  passes to `claude --model`. A model name is provider vocabulary living in `core` + every workflow
  — a codex task would be handed `--model opus` (§3a).

Milestone 3 needs a second CLI to drop in with **the control plane behaving identically per CLI**
(it stores a CLI name and renders CLI-agnostic text, but runs no CLI-specific logic) and without a
second copy of the launcher's control flow. This ADR settles the seam. It is **design only** — no
code lands here; it is the contract the M3 implementation slices build against.

The comments already scattered through those modules ("M3 revisits for other CLIs",
"claude-specific renderer", "claude-specific wiring") are the informal version of this decision;
this ADR makes it the plan of record.

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
| **parse hook payload** (turn-flip background-task gating) | `background_tasks` JSON on stdin | codex hook payload schema (verify) |
| **write MCP client config** | `panopticon-mcp.json` (`{"mcpServers": …}`) via `--mcp-config` | `[mcp_servers.panopticon]` in `config.toml` |
| **render workflow-overview / system prompt** | `--append-system-prompt <overview>` | `$CODEX_HOME/AGENTS.md` (our config dir) — **not** the repo's `/workspace/AGENTS.md` |
| **build launch argv incl. resume** | `claude --dangerously-skip-permissions [--continue \| --model M PROMPT]` | `codex …` first-run vs `codex resume --last`/session-id |
| **trust / first-run pre-accept** | `.claude.json` onboarding + trust + cost keys | codex trust / sandbox-approval seed |
| **auth env var(s) + missing-auth check** | `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` / codex login token |
| **resolve tier → concrete model** | tier `"opus"` → `claude --model opus` | tier → a codex model id |

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

The resolved CLI drives three host-side choices at spawn:

1. the **base-image variant** (§4);
2. the **config-dir mount path** — the runner mounts a per-task volume at `CONFIG_MOUNT`
   (`local_runner.py:59`, today hard-coded `"/home/panopticon/.claude"`) that persists session
   history across respawn; that path (and the **first-spawn probe** of it, `test_local_runner.py`)
   must derive from the resolved CLI (`$CODEX_HOME` for codex), or **resume silently breaks** —
   `--continue` / `codex resume --last` has nothing to resume from (§4a);
3. the `PANOPTICON_AGENT_CLI` env var the launcher reads.

**Auth is *not* a per-CLI host-side pick.** The runner injects the repo's **entire** `env_file`
wholesale (`--env-file`, `local_runner.py:214`) — it never selects individual variables. The
env-file already carries whatever the CLI needs (`CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`; the
ADR 0007 secret model generalizes, no new mechanism). The CLI drives at most a spawn-time
*presence* check; the actual **missing-auth check is the in-container adapter seam** (the table
row), which knows which var its CLI requires.

This is a schema change (`Repo`/`Task` rows + an Alembic migration) owned by the **selection-seam
slice**, not this ADR.

#### 3a. Model naming is a tier, resolved per CLI

`Workflow.default_model` / `Task.starting_model` become an **abstract tier** the control plane
stores and passes through opaquely (it already flows as an opaque `PANOPTICON_STARTING_MODEL`
string — no core logic inspects it). The **adapter** maps the tier to its CLI's concrete model on
the launch argv (`"opus"` → `claude --model opus`; a codex tier → a codex model id). Workflows keep
declaring a tier; no workflow or `core` code names a provider's model on the wire. This resolves
the model-naming leak the Context calls out without adding CLI-specific behavior to the control
plane — the tier vocabulary is CLI-agnostic, the concrete mapping lives in `container/`.

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

#### 4a. The per-task config-volume mount path is per-CLI

Called out in §3 but worth stating as its own deliverable, since it's the non-obvious host-side
change that breaks resume if missed: the runner mounts a **per-task config volume** at
`CONFIG_MOUNT` (`local_runner.py:59`). This volume is what makes resume work at all — session
history persists in it across respawn/recreate, and the launcher's first-run-vs-resume probe reads
it. Today it is hard-coded to claude's `/home/panopticon/.claude`. The mount path — and the
**first-spawn gate** that probes the volume (`test_local_runner.py`) — must be derived from the
resolved CLI's config dir (`$CODEX_HOME` for codex). Owned by the selection-seam / codex-adapter
slices; the ADR names it so it isn't discovered late.

### 5. The Codex mapping — concrete, with flags to verify

Codex satisfies every seam, using these current facts:

- **Config dir:** `~/.codex`, overridable via `CODEX_HOME`. Config file `config.toml`.
- **Skills / prompts:** custom prompts live under `~/.codex/prompts/<name>.md` — the skill and
  operation renderers target that dir instead of `.claude/commands/`.
- **MCP:** codex has `[mcp_servers.<name>]` tables in `config.toml` — the panopticon server is one
  entry pointed at `<service_url>/mcp`.
- **Hooks:** codex has a hooks system with `SessionStart` / `UserPromptSubmit` / `Stop` /
  `PreToolUse` / `PostToolUse` events, configured in `config.toml` — the same turn-flip contract
  (Slice 4) maps across, invoking `python -m panopticon.container.hook`. Two claude specifics ride
  on these hooks and need a per-CLI answer, not just a schema translation: (a) the per-turn
  **briefing and provisioning-nudge** are delivered by claude injecting a `UserPromptSubmit` hook's
  **stdout into the agent's context** (`hook.py:172`) — codex may not feed hook output back into
  context (flag 6); (b) the `PreToolUse`/`PostToolUse` flip is matched to **`AskUserQuestion`**, a
  claude-specific tool with no codex analogue — the "agent is asking the user" turn state needs its
  own codex trigger or a documented no-op (flag 7).
- **System prompt:** codex has no `--append-system-prompt`; it layers instructions from an
  `AGENTS.md` **hierarchy** — a codex-home file (`$CODEX_HOME/AGENTS.md`) merged with the project's
  root `AGENTS.md` (and nested ones). The workflow overview goes in **our** `$CODEX_HOME/AGENTS.md`,
  which the container controls, so it composes *on top of* the repo's own `/workspace/AGENTS.md`
  rather than clobbering it. **We never write the repo-root `AGENTS.md`** — the working tree is the
  task's clone of the repo and must stay the repo's. (A dedicated `experimental_instructions_file`
  in `config.toml` pointing at a file we own is an equally-additive alternative if it proves more
  robust — see the flags below.) This keeps the additive property the claude
  `--append-system-prompt` seam has.

  Note the seam could be **unified** on a global-instructions-file mechanism for *both* CLIs:
  claude has the same shape of global memory file — `~/.claude/CLAUDE.md` (and it reads `AGENTS.md`
  through a `CLAUDE.md` import, exactly as this repo's `CLAUDE.md` = `@AGENTS.md`), which composes
  on top of the repo's own `CLAUDE.md`/`AGENTS.md` the same way codex's home file does. The ADR
  keeps `--append-system-prompt` for claude anyway, because it is a **strictly stronger guarantee**:
  the overview lands in the actual system prompt every launch, ephemerally, with no on-disk file
  that could collide with the repo's memory — whereas codex has no such flag, so the file is its
  *only* option. Both are the same adapter method ("deliver the overview into the agent's context"),
  so which mechanism each CLI uses is an implementation choice inside its adapter, not a change to
  the seam's contract.
- **Resume:** `codex resume --last` (or a session id) is the `--continue` analogue; the launcher's
  first-run-vs-resume decision keeps its shape, only the argv changes.
- **Auth:** `OPENAI_API_KEY` (or the codex login token) instead of `CLAUDE_CODE_OAUTH_TOKEN`.

**Flags to verify during the codex-adapter slices** (not blocking this ADR; each is a small
spike the implementer resolves against the installed codex version):

1. MCP transport — HTTP vs stdio support (we serve HTTP today; if codex is stdio-only we front it
   with a local proxy or add an stdio MCP entrypoint).
2. ~~The exact codex **hooks config schema** and payload shape (for the turn-flip callback's
   background-task gating — the `background_tasks` analogue).~~ **Resolved (M3.6):** hooks live under
   a `[hooks]` table keyed by event, each an array of groups whose `hooks` array holds
   `{type = "command", command = …}` (`[[hooks.Stop]]` → `[[hooks.Stop.hooks]]`); `Stop` /
   `UserPromptSubmit` take no `matcher`. The `Stop` stdin payload is `session_id` /
   `transcript_path` / `cwd` / `hook_event_name` / `model` / `permission_mode` / `turn_id` /
   `stop_hook_active` / `last_assistant_message` — **no `background_tasks` analogue**, so the flip
   degrades to the plain turn hand-back (claude's exact behaviour when the field is absent). The
   adapter still parses a `background_tasks` array the same way, so a future codex build that adds
   one lights up the gate with no code change.
3. The `CODEX_HOME` config-dir override and its precedence.
4. The **instructions-merge precedence** — confirm `$CODEX_HOME/AGENTS.md` (or
   `experimental_instructions_file`) layers *additively* on top of the repo's root `AGENTS.md`
   rather than either one silently winning, so the workflow overview and the repo's own guidance
   both reach the agent. Pick whichever mechanism guarantees the workflow overview is present
   without touching the working tree.
5. The unattended / **skip-approvals sandbox** flag (the `--dangerously-skip-permissions`
   analogue — codex runs headless in a throwaway container on a per-task clone).
6. ~~Whether codex **feeds hook stdout into the agent's context** (as claude does for
   `UserPromptSubmit`).~~ **Resolved (M3.6): yes** — a `UserPromptSubmit` hook's plain-text stdout
   is added to the agent as extra developer context, so the per-turn briefing + provisioning nudge
   ride the same channel as claude, no alternate needed. (`Stop` is the opposite: plain-text stdout
   is invalid there — JSON only — but our callback prints nothing on the stop path, so it's fine.)
7. ~~The codex trigger for the **"agent is asking the user" turn state** — the `AskUserQuestion`
   PreToolUse/PostToolUse flip has no direct codex analogue.~~ **Resolved (M3.6):** codex has no
   `AskUserQuestion` tool (and `Stop`/`UserPromptSubmit` take no `matcher`), so we wire no
   `PreToolUse`/`PostToolUse` pair — the turn stays on the agent until the next `Stop`, the accepted
   documented degradation.

### 6. The determinism invariant holds

Adapters live **only** in `container/`, the sole LLM-bearing package. The seam adds **no** LLM
calls to the control plane, and the deterministic turn mechanism (the task service's `set_turn` /
responsibilities / state machine) is unchanged — only the CLI-specific **hook wiring and payload
parsing** are per-adapter.

The precise invariant is: **the control plane runs no CLI-specific logic.** It is not that it
"never learns which CLI a container runs" — it plainly does (§3 adds `Repo.agent_cli` /
`Task.agent_cli` columns it stores and serves, and it passes an abstract model **tier** through).
But those are opaque data: the task service stores a CLI name and a tier string, renders
CLI-agnostic text, and branches on neither. No control-plane code path forks on the CLI; the CLI
name only selects an adapter, host-side, at spawn. That is what keeps the determinism invariant
intact while the system genuinely supports more than one CLI.

## Consequences

- **Enables M3.** A reviewer can implement the refactor and the Codex adapter from this ADR without
  further design decisions. The seam is the stable contract every later M3 slice builds against.
- **Contained blast radius — but not container-only.** The bulk is `container/` (the adapters).
  Host-side, the runner's **image selection *and* config-volume mount path** (§4a) become
  CLI-derived. `core` gains the `Repo`/`Task.agent_cli` columns (additive migration) and the
  abstract model **tier** pass-through (§3a). `taskservice` and the dashboard are untouched. No
  control-plane code path forks on the CLI (§6).
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
