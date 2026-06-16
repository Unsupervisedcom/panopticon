# Architecture

This document turns the decisions in `docs/decisions/` (ADRs 0001–0008) into a concrete
component design: the three-role topology, the interfaces and their adapters, the workflow
model, the data model, and the end-to-end task lifecycle. The cloude-cade prototype is the
spec-by-example (`docs/PARITY.md`); the milestones (`docs/GOALS.md`) are the trajectory.

Read the ADRs for *why*; read this for *what* and *how it fits together*.

## 1. Principles

1. **Core-and-adapters.** A UI-agnostic, backend-agnostic core depends only on
   **interfaces** (abstract Python base classes); concrete **adapters** implement them, and
   dependencies point inward. Storage, dashboard, artifacts, workflows, and execution are
   all replaceable behind interfaces. (This is the pattern often called *hexagonal* or
   *ports-and-adapters*; we say interface/adapter/boundary rather than "port".)
2. **Determinism invariant.** The control plane and all operator-facing surfaces — task
   service, session service, terminal controller/dashboard — make **zero LLM calls**. Every
   LLM call happens **inside a task container**. (§3.)
3. **Modularity for sustained development.** Boundaries are enforced by *process separation*
   where it matters (ADR 0008), not just convention, so the seams can't quietly rot.
4. **Workflows are data + code, authored with agent help.** The lifecycle is a Python class,
   not control flow baked into the engine (ADR 0004).

## 2. System overview

```
  ┌──────────────────┐          ┌──────────────────────────────────────────────────────────┐
  │ Terminal         │          │ TASK SERVICE — control plane                             │
  │ controller       │◀─ REST ─▶│ deterministic · sole DB authority                        │
  │  └ dashboard     │          │                                                          │
  │     (Textual)    │          │ Store (SQLite) · Workflow registry                       │
  └───────┬──────────┘          │ Lifecycle engine: state machine,                         │
          │                     │   turn, responsibility gating                            │
          │ tmux attach (local) │ REST API · MCP surface (artifacts+tools)                 │
          │ ssh+attach (M5)     └─────────▲─────────────────────────────────▲──────────────┘
          │                               │                                 │
          │                               │ REST: register, pull work       │
          │                               │                                 │ MCP (+REST)
          │                     ┌─────────┴────────────┐       ┌────────────┴──────────────┐
          │                     │ SESSION SERVICE      │       │ TASK CONTAINER(S)         │
          │                     │ (runner) — host      │       │ agent (LLM) + workflow    │
          └────────────────────▶│ process. On host:    ├spawns─▶ skills; entrypoint:       │
                                │ · spawn/stop ctrs    │       │ connect & stay (live-     │
                                │ · own host tmux      │       │ ness); set-slug hook      │
                                │ · inject secrets     │       │ (M2: planner+impl =       │
                                │ · run composed img   │       │   2 ctrs, 1 session)      │
                                └──────────────────────┘       └───────────────────────────┘
  ── host processes · deterministic · no LLM ──                ── only LLMs run ──
```

Three deterministic roles (ADR 0008), plus task containers where all LLM work lives:

| Role | What it is | Talks to task service via |
|---|---|---|
| **Task service** | Control plane: state authority, integrity, workflow registry, lifecycle engine | — (it *is* the service) |
| **Session service (runner)** | Per-machine executor: spawns/stops containers + tmux, injects secrets, runs composed images | REST (registers, pulls work, reports status) |
| **Terminal controller** | The `panopticon` CLI: owns the TTY, runs the dashboard, switches tmux on `t` | REST (dashboard is a REST client) |
| *Task container* | The agent + workflow skills; the **only** LLM-bearing component | MCP (artifacts + task tools) + some REST |

## 3. The determinism invariant

**The task service, session service, and terminal controller never call an LLM. All LLM
calls happen inside task containers.** This is a hard architectural rule, not a guideline.

Why:
- **Testability & reproducibility** — the control plane and dashboard are pure deterministic
  software, unit-testable without model access or nondeterminism.
- **Cost & isolation** — model usage is bounded to containers, where secrets and credentials
  are already scoped per repo (ADR 0007).
- **Clear mental model** — the core is *mission control*, not an agent.

**Implication for workflows (refines ADR 0004).** A workflow spans the determinism boundary,
so its responsibilities split by *where they run*:

- **Control-plane side (deterministic, in the task service):** the workflow class's
  declarative members (state set, transitions, foreground/background classification,
  responsibility *definitions*) and any deterministic imperative methods (compute next state, list cleanup
  steps, construct a non-reasoning forge request). No LLM.
- **Container side (LLM, in the task container):** the workflow's **skills** — the
  agent-driven procedures it contributes (planning, implementing, `babysit-ci`'s
  diagnose-and-fix loop). The agent executes these and calls back to the task service
  (REST/MCP) to record results and request transitions.

So a workflow's "imperative behavior" (ADR 0004) is delivered mostly as **in-container
skills**, with the control plane holding only the deterministic state machine and the
catalogue of which skills exist. *(This sharpens ADR 0004's "imperative methods" language;
0004 should be read through this boundary.)*

## 4. Components

### 4.1 Task service (control plane)

The single authority over task state (ADR 0006). Responsibilities:
- **Owns the store** (ADR 0001) — the only direct DB reader/writer; everything else
  mutates state *through* it, which provides the serialization that gives integrity.
- **Hosts the workflow registry** (ADR 0004) — on startup, loads workflow classes via
  path-based registration; dispatches to the active workflow per task.
- **Drives task lifecycle** — calls the active workflow to apply transitions, enforce
  responsibility gating, and maintain the `agent`/`user` turn (workflows supply only the
  fg/bg classification).
- **Exposes two interfaces:** a **REST API** (dashboard, session service, in-container
  skills) and an **MCP surface** (in-container agents: artifacts as resources, task
  operations as tools). See §5.
- **Coordinates runners** — assigns tasks to session services; tracks which container is
  working on which task via their persistent connections.

Network-capable from day one (M4 web dashboard, M5 remote runners both require it).

### 4.2 Session service (runner)

The execution-backend ABC realized as a separate **host process** (ADR 0008). Per machine. It:
- spawns/stops **task containers directly on the host Docker daemon** (the runner is on the
  host, so no Compose / Docker-out-of-Docker socket mount) and owns the **host-level tmux**;
- owns **one tmux session per task** — *not inside the agent container*; its panes exec into
  the task's container(s), so a task with separate planner + implementer containers (M2)
  shares one session;
- **builds/selects the composed image** for a task (base → workflow → repo, ADR 0005);
- **injects per-repo secrets** at launch (env vars + creds mount, ADR 0007);
- **provisions the worktree** once the slug is set — observing the task over its pull loop, it
  builds the slug-named worktree on its host and repoints the container into it, then runs the
  workflow's provisioning (ADR 0010);
- **registers with the task service** and reports session status.

Concrete adapters behind the same interface, over time: local Docker+tmux (now), remote
machine (M5), non-container (later).

### 4.3 Terminal controller

The `panopticon` CLI the user runs (a host process). It owns the TTY and is a REST client of
the task service. It:
- ensures the task service + runner host processes are running, then runs the **dashboard**
  (Textual, the presentation adapter — ADR 0002) in a tmux session;
- runs as a **session supervisor** (ADR 0009): the dashboard lives in its own tmux session, and
  on **`t`** it hands the terminal to the selected task's tmux via plain `tmux attach` (host
  tmux; remotely via ssh to that runner), **rejoining the dashboard on detach**. Switching is
  always detach→attach, never `switch-client`, so the same loop reaches a remote task over ssh;
- the dashboard is also an **input surface** (idea capture, promotion, transitions) — all via
  REST (PARITY §5).

## 5. Interfaces & protocols

| Client | Protocol | Used for |
|---|---|---|
| Terminal controller / dashboard | **REST** | queries (task list/detail), commands (create idea, promote, advance, drop), runner/status views |
| In-container agent | **MCP** | artifacts as resources (plan/notes, ADR 0003); task operations as tools (set slug, advance, resolve responsibility, append log) |
| In-container skills | **REST** (some) | operations not natural as MCP tools, or when a skill needs the broader API |
| Session service | **REST** | register as runner, pull assigned work, report session lifecycle/health |

The MCP surface is *fronted by the task service* (ADR 0003/0006) — agents reach artifacts and
task tools through it rather than touching the DB or filesystem directly.

## 6. Interfaces & adapters

Every interface is a Python ABC in the core; adapters live in the owning component.

| Interface | ABC responsibility | Adapter now | Future adapters | ADR |
|---|---|---|---|---|
| **Store** | persist tasks, repos, history; enforce transitions | SQLite | Postgres | 0001/0006 |
| **Workflow** | define a lifecycle (states, responsibilities, skills, deterministic methods) | parity, free-form | user/agent-authored | 0004 |
| **Execution backend** (session service) | spawn/stop a task's session; inject secrets; run image | local Docker+tmux | remote, non-container | 0005/0008 |
| **Artifact store** | read/write per-task files (plan, notes) | local filesystem | object storage | 0003 |
| **Presentation** | render + drive the system | Textual TUI | web UI, other-lang dashboard | 0002 |
| **Agent runner** *(emerges at M2/M3)* | invoke an agent CLI with a model/role | `claude` | other CLIs, per-stage models | GOALS M2–M3 |

Secrets are *not* a backend-agnostic interface (ADR 0007 deliberately dropped that): they are
per-repo env vars + a creds mount injected by the session service.

## 7. The workflow model (concrete shape)

A workflow is a concrete subclass of the workflow ABC (ADR 0004), loaded by path-based
registration. Sketch of the ABC's two faces:

- **Declarative members** (data the control plane reads):
  - `states` (nested `State` classes) and their `transitions` (class refs or label strings);
  - per-state `turn_on_enter` (who holds the turn on entry) and `advanced_by` (who transitions
    out: `USER` — the default — / `AGENT` once satisfied);
  - `responsibilities(state)` → the agent's obligations for that state. Entering a state seeds
    them onto the new history entry, all `PENDING` (a promise); the agent fulfils each one at a
    time (`MET`, or `FAILED` with a comment) and the next advance is gated on all being
    resolved;
  - `skills()` → the catalogue of workflow-specific skills exposed in the container (e.g.
    `babysit-ci`), on top of the core operations (`advance`, `drop`, and `report` a
    responsibility's status). A free-form workflow contributes few or none.

The resolved state graph is built and validated **lazily on first query, then cached** (à la
an ORM's mapper configuration), not at construction.
- **Deterministic methods** (called by the control plane; no LLM):
  - provisioning steps, cleanup steps (composed on top of the core's agnostic teardown),
    image-layer contributions (ADR 0005), forge requests that need no reasoning.
- **In-container skills** (executed by the agent; may use LLM and run git/`gh`):
  - the agent-driven procedures named by `skills()`; these are shipped into the container and
    call back via REST/MCP.

**Two-tier commands (ADR 0004):** core operations exist for every workflow; workflow skills
exist only if the active workflow defines them — so a forge-less workflow simply has no
`babysit-*`. The container's command surface and the dashboard enumerate skills *from the
active workflow*, not a fixed global list.

**Local vs remote git (ADR 0004):** local branch creation/naming and worktrees are core
(agnostic); only remote forge integration (PR/CI/merge, ADOPT checkout) is workflow-specific.

## 8. Data & artifacts

### 8.1 Entities (store, ADR 0001)

- **Repo** — first-class (ADR 0007): identity, default base, association to its env config +
  creds volume (references, **never secret values**).
- **Task** — stable internal **id** (generated at creation), `repo_id`, `workflow`, current
  `state`, **`turn`** (`agent`/`user`), git refs (branch/worktree), optional
  forge refs (PR), and an **optional `slug`** (see §8.3). (Per-state `turn_on_enter`/`advanced_by`
  live on the `State` classes, not the task.) The task carries behavior over **its own
  record** — fulfilling the responsibilities it promised on entry and reporting which remain
  outstanding; the *rules of the state machine* live on the workflow.
- **History** — per-task log, one entry recorded when the task **enters** a state. Each entry:
  timestamp, from/to state, `trigger`, and that state's **responsibilities, seeded `PENDING`
  on entry and fulfilled one at a time** (each with its `status` and, if `FAILED`, the agent's
  comment). Entries are otherwise immutable; only their responsibility list mutates as promises
  are kept. This is cloude-cade's `** Log` as structured rows — but responsibilities are
  **agent-only** obligations (a deliberate divergence: cloude-cade's DoD could include user
  items; here user actions just drive transitions directly).

### 8.2 Artifacts (ADR 0003)

Freeform per-task files — the **plan** and **notes** — live on the filesystem in a per-task
directory resolvable from the task id, e.g. `…/tasks/<task-id>/plan.md`. The DB holds only
references. The same bytes are reachable three ways: MCP resources (agent), the filesystem
(text editor), and the dashboard. A **single shared resolver** maps `task-id → directory →
MCP URI` for all three (the ADR 0003 deferred item — now keyed off the internal task id).

### 8.3 Identity vs. slug (refines cloude-cade)

Unlike cloude-cade (slug chosen host-side at promote), **the slug is decided in the
container.** Task identity is the internal id, generated at creation and independent of any
slug. The slug is a human-friendly label, **nullable until the agent sets it** via a task
tool/REST. A container **hook** detects an unset slug and instructs the agent to set one. This
decouples identity from naming and lets the agent (which has the most context) choose the
slug. *(Refines PARITY §14's "YYYY-MM-DD-<slug>" id.)*

The task's **worktree/branch are named from the slug**, so the slug must be set before the
core can create the (always-required) worktree — and therefore before any workflow
provisioning that needs the branch (e.g. the parity workflow's PR). See the §9 lifecycle.

### 8.4 Secrets (ADR 0007)

Per repo: an env config (API-key-style secrets) and an optional creds volume (OAuth token
files). The session service injects only the task's repo's secrets at launch — env vars +
mounted creds. Values never enter the DB, artifacts, or image layers.

## 9. End-to-end task lifecycle

1. **Idea → task.** A user captures an idea / promotes it via the dashboard (REST). The task
   service creates a Task row (internal id, chosen workflow, `repo_id`), in the workflow's
   initial state, with the turn assigned appropriately. No slug yet.
2. **Assign & spawn.** The task service assigns the task to a session service (runner). The
   runner builds/selects the composed image (ADR 0005), injects the repo's secrets (ADR 0007),
   creates the task container (sibling, DooD) and its tmux session, and starts the agent. The
   container begins on a **read-only checkout** of the repo (there's no slug, hence no worktree,
   yet) for the agent to plan against; step 5 upgrades it to the writable worktree.
3. **Connect & register.** The container entrypoint **connects to the task service and stays
   connected** — registering that this container is working on this task (liveness). It loads
   the workflow's in-container skills (from the active workflow) on top of the core operations.
4. **Slug.** A hook notices the slug is unset and instructs the agent to set one via a task
   tool; the task service records it.
5. **Worktree & provisioning.** The **session service**, observing the task over its pull loop
   (ADR 0010), sees the slug land and creates the task's **worktree/branch** on the host where
   the container runs — always required, named from the slug, so it cannot precede step 4 — then
   repoints the container's working path to it (the agent `cd`s in). The active workflow's
   **provisioning** then runs (e.g. the parity workflow opens its PR); because it needs that
   branch, PR creation is transitively gated on the slug too.
6. **Work.** The agent plans/implements; artifacts (plan/notes) flow over MCP; the agent runs
   workflow skills (e.g. `babysit-ci`) that may use `gh`/git and call back over REST/MCP to
   request transitions. The task service deterministically enforces the workflow's state
   machine and responsibility gating, flips the turn per the fg/bg classification, and appends history.
7. **Observe & steer.** The dashboard reflects state/turn/history live (REST); the user
   presses `t` to drop into a task's tmux and back.
8. **Terminal states & cleanup.** On COMPLETE/DROPPED (or the workflow's terminals), cleanup
   runs the core's agnostic teardown (tmux/worktree/branch) plus the workflow's specific
   steps; the session service stops the container/session.

## 10. Deployment topology (ADR 0008)

- **Milestone 1 (local):** **three host processes** — `task-service`, the `session-service`
  runner, and the `panopticon` terminal controller — plus **N task containers** the runner
  spawns directly on the host Docker daemon. No Compose, no Docker-out-of-Docker (the runner
  is on the host). The runner owns the host tmux server.
- **Milestone 5 (remote):** run a `session-service` **host process per machine**, each pointed
  at the central `task-service` (still single and the sole DB writer; runners are REST
  clients). The terminal controller reaches a remote task's tmux over ssh. Adding a machine =
  starting a runner there and registering it; no control-plane change.

## 11. Suggested module layout (Python)

```
panopticon/
  core/          # interfaces (ABCs), domain models, state classes + the Workflow state machine — no LLM/UI/DB
  taskservice/   # control plane: REST + MCP servers, store adapter (SQLite), workflow loader
  sessionservice/# runner: execution-backend adapter (Docker+tmux), image build/compose, secret injection
  terminal/      # `panopticon` CLI + dashboard (Textual) — presentation adapter
  workflows/     # built-in workflow classes (parity, free-form) on a registered path
  container/     # in-container entrypoint, hooks (slug, turn), skill definitions, REST/MCP client
```

The determinism invariant maps onto packages: only `container/` (and user workflow *skills*)
may invoke an LLM; `core/`, `taskservice/`, `sessionservice/`, `terminal/` may not.

## 12. Mapping to milestones

| Milestone | Primarily touches |
|---|---|
| **M1** parity + free-form | core, taskservice, sessionservice (local), terminal, two workflow classes, per-repo secrets |
| **M2** planner vs implementer | agent-runner interface (per-stage model/role); workflow `skills()`; container agent invocation |
| **M3** other CLIs | agent-runner adapters; base-image variants (ADR 0005); container entrypoint |
| **M4** web dashboard | second presentation adapter consuming the existing REST API |
| **M5** remote execution | session-service deployed per machine; runner registration/auth; artifact access via MCP |

## 13. Cross-cutting open questions (carried from the ADRs)

- **Runner registration/discovery & work assignment** — how runners announce themselves,
  report capacity/health, and get assigned tasks (runner-initiated/pull, NAT-friendly for M5;
  ADR 0010 commits the session service to *pull* for observing task state, e.g. the slug). (ADR 0008/0010)
- **Process supervision** — with Compose gone, how the three host-process daemons are started
  and kept alive (terminal-controller-on-demand, systemd, or a small supervisor). (ADR 0008)
- **Container → host-service addressing** — how a task container reaches the host task service
  (host gateway / injected URL). (ADR 0008)
- **Inter-process auth & transport** — trivial on one host; real once remote (M5). (ADR 0006/0008)
- **Failure/restart reconciliation** — task service or runner restart vs. in-flight
  containers. (ADR 0008)
- **Image matrix, storage/registry, rebuild triggers, layer order** — (ADR 0005)
- **Declarative/imperative discipline in workflow classes** — keep the state machine in
  declarative members, not buried in methods. (ADR 0004)
- **At-rest secret protection & remote secret delivery** — (ADR 0007, M5)
- **Artifact concurrency** (agent-via-MCP + editor + dashboard) and drift detection. (ADR 0003)
