# 0004 — The workflow abstraction

- Status: Accepted
- Date: 2026-06-11
- Amended: 2026-06-11 — reconciled with the determinism invariant (ADR 0008 /
  ARCHITECTURE.md §3): a workflow's "imperative" behavior splits across the determinism
  boundary. See "Where imperative behavior runs" below.
- Amended: 2026-06-12 — terminology + model: the "ball" is now the **turn**; a state's
  Definition-of-Done is now the agent's **responsibilities** (agent-only, unlike cloude-cade);
  each resolves to a **status** (`PENDING`/`MET`/`FAILED`, a `FAILED` one needs a comment) and
  the resolved set is recorded per turn in history.
- Amended: 2026-06-14 — responsibilities are a **promise made on entry**: entering a state
  records a history entry seeded with that state's responsibilities, all `PENDING`; the agent
  fulfils them **one at a time** (mutating that entry) and a later advance is gated on all
  being resolved. Fulfilling a promise is **`Task` behavior** (it touches only the task's own
  record); the **`Workflow`** owns the state-machine rules and the advance gate. Workflow
  resolution/validation is **lazy and cached** (on first query, à la an ORM mapper config).
- Deciders: Charlie Scherer

## Context

cloude-cade hardcodes one lifecycle (PLANNING → ITERATING → REVIEW → MERGING →
COMPLETE) directly in code (`bin/cloude_stages.py` plus per-stage slash commands and
hooks). The primary motivation for the rewrite (GOALS.md) is **flexible workflows**:
the cloude-cade flow becomes *one* workflow among several, and Milestone 1 requires a
second "free-form" workflow as proof the lifecycle is configurable, not baked in.

The PARITY categorization makes the boundary concrete. The dominant signal across the
whole checklist was a repeated split between **workflow-specific** and
**workflow-agnostic** behavior:

- Marked *workflow-specific* (C / "workflow specific"): the state set itself, the
  `:SKIP_REVIEW:` bypass, the per-stage audit semantics, **remote** `gh`/forge PR
  integration, ADOPT mode, the plan-accepted lifecycle hook, parts of `/finalize`, and
  workflow-specific parts of cleanup.
- Kept *workflow-agnostic* (K): **local git branch creation/naming**, worktree
  management and teardown tiers, idempotent cleanup mechanics, the `agent`/`user`
  **turn** tracking (flagged the single most important feature), task
  identity/slug, the per-task container/tmux plumbing, and the secret store / repo
  entity.

We need to define what a **workflow** *is* so that the core stays agnostic and each
workflow owns its specific behavior.

## Decision

A **workflow** is a first-class, pluggable unit behind a **workflow interface**. The
interface drives tasks generically; the active workflow supplies everything specific to a
given way of working. The cloude-cade flow and the free-form flow are two
implementations of this interface.

### A workflow is a Python class implementing an abstract interface

A workflow is a **concrete subclass of an abstract base class** — the workflow interface
*is* that ABC. Workflows are **code, not a data file or DB rows**: a pure-data
definition can't capture everything a workflow owns (opening a PR, resolving a CI
failure, workflow-specific teardown are imperative), and expressing the whole thing in
Python keeps the declarative and imperative parts in one cohesive, type-checked unit.

Users are expected to author these classes **with the help of an agent** — fitting for a
tool whose whole purpose is running coding agents. "Ask an agent to write me a workflow
that does X" is the intended authoring path, not hand-editing a config schema.

Within the class, the two kinds of responsibility are expressed differently:

- **Declarative members** (attributes / properties returning data) — the parts that are
  data:
  - the **state set and legal transitions** (the state machine);
  - per-state **`turn_on_enter`** (who holds the turn on entry) — the agnostic turn-tracking
    starts from it;
  - per-state **`advanced_by`** — who transitions out: `USER` (the default — e.g. approving a
    plan) or `AGENT` (the agent does so once satisfied);
  - the **responsibilities** per state — the agent's obligations to fulfil before handing
    the turn back. Entering a state seeds them onto that state's history entry, all `PENDING`
    (a promise); the agent then resolves each to a **status** (`MET`, or `FAILED` with a
    comment) one at a time, and a later advance is gated on all being resolved.
- **Imperative behavior** — what the workflow *does* at defined moments. This is delivered
  in two forms depending on where it must run (see "Where imperative behavior runs" below):
  - **provisioning** — forge-side setup a task needs on entry (PR creation, ADOPT-style
    checkout of an existing PR, image layers — see ADR 0005). *Local* branch creation,
    naming, and worktree setup are **core**, not here;
  - **remote VCS / forge integration** — PR creation, CI watching/fixing, merge-queue
    shepherding (the `babysit-*` behaviors). Workflow-specific because it talks to a remote
    forge; local git is not. *Largely delivered as in-container skills (see below).*
  - **resolving responsibilities** — the agent judges each of the current state's
    responsibilities `MET` or `FAILED` (with a comment) before handing the turn back
    (in-container);
  - **lifecycle hooks** — e.g. on-plan-accepted, on-transition;
  - **workflow-specific cleanup** — the teardown steps unique to this workflow, layered
    on top of the agnostic teardown the core provides.

A workflow with an almost-empty spec and no imperative behavior is the **free-form
workflow** (Milestone 1): a minimal state set, no rigid gates. Its existence is the test
that the engine has no hardcoded lifecycle.

### Where imperative behavior runs (determinism boundary)

The determinism invariant (ADR 0008 / ARCHITECTURE.md §3) requires that the control plane
make **no LLM calls** — all LLM work happens inside task containers. A workflow therefore
**spans that boundary**, and its imperative behavior splits by *where it must run*:

- **Control-plane side (deterministic, in the task service):** the declarative members
  above, plus imperative methods that need no reasoning — computing the next state, listing
  cleanup steps, contributing image layers, constructing a forge request. The task service
  calls these directly.
- **Container side (LLM, in the task container):** the workflow's **skills** — agent-driven
  procedures such as planning, implementing, `babysit-ci`'s diagnose-and-fix loop, and
  resolving its responsibilities. The agent runs these and calls back to the task service
  over REST/MCP to record results and request transitions.

So the `babysit-*` "handlers" are **not** control-plane methods that call an LLM; they are
**in-container skills the workflow contributes**, with the control plane holding only the
deterministic state machine and the *catalogue* of which skills exist. Read every
"imperative method" above through this split; ARCHITECTURE.md §3 and §7 are authoritative on
the boundary.

### Division of responsibility

| Concern | Owner |
|---|---|
| State set, transitions, fg/bg classification, transition policy | **Workflow** (declarative) |
| Per-state responsibilities (agent obligations) + their `status` | **Workflow** (declarative) |
| **Remote** VCS/forge integration: PR creation, CI babysit, merge babysit, ADOPT checkout | **Workflow** — mostly **in-container skills** (LLM); deterministic forge requests may be control-plane |
| Plan-accepted / on-transition lifecycle hooks | **Workflow** (control-plane method, deterministic) |
| Workflow-specific cleanup steps | **Workflow** (control-plane method, deterministic) |
| Driving the state machine; persisting state + history | **Core** (store, ADR 0001) |
| `agent`/`user` turn tracking (the *mechanism*) | **Core** — workflow only supplies the fg/bg classification |
| **Local git: branch create + naming**, worktree create/teardown tiers, idempotent cleanup | **Core** |
| Task identity / slug, repo entity, per-repo secrets | **Core** |
| Container/exec plumbing (image *layers* are workflow/repo-configurable, ADR 0005) | **Core**, parameterized by workflow |
| Dashboard rendering + idea input | **Core** (presentation, ADR 0002) |

The rule of thumb: **the core knows *that* there is a state machine with a held-by turn
and a history; the workflow knows *what* the states are, *when* the turn flips, and
*how* the imperative steps happen.**

### Commands / skills are workflow-determined

The prototype's slash commands are not hardcoded transitions. They split into two tiers:

1. **Core operations** — present for every workflow, routed to the active workflow's
   spec/handlers. E.g. `advance` asks the workflow for the next state and its responsibilities;
   `drop` is a generic transition to the workflow's terminal abandon state.
2. **Workflow-specific skills** — exist **only if the active workflow defines them**.
   The `/babysit-ci` and `/babysit-merge` skills are the prime example: they are remote
   forge/CI integration, which is workflow-specific (per the local/remote split above),
   so a workflow without forge integration — e.g. the free-form workflow — **does not
   expose them at all**. They are not stubs that no-op; they are simply absent.

So the **set of available skills is part of a workflow's definition**, not a fixed
global menu. The dashboard and the in-container command surface present whatever skills
the active workflow contributes, on top of the core operations.

### Discovery, loading, and trust

Workflow classes are loaded by a **task service** — a long-running service that wraps
the task database (the store interface, ADR 0001) and is the core's home for
orchestration state. On startup, the task service **loads all workflow classes via a
path-based registration mechanism** (a configured set of paths/modules it imports and
registers against the workflow ABC). The active set of workflows is whatever the task
service discovered at startup; adding a workflow means placing its class on a registered
path. (The task service is itself a core architectural component, specified in ADR 0006;
it is also the natural thing the dashboard and the in-container agents talk to.)

Because workflows are arbitrary Python executed at orchestrator privilege (and may be
agent-authored), panopticon does **not** sandbox workflow code. Instead it treats
**reviewing workflow files as the user's responsibility, proportional to the stakes** —
when operating where the stakes warrant it, users are expected to review a workflow
class before trusting it, the same as any other code they choose to run. This is a
deliberate decision to keep the system simple; heavier isolation can be revisited if a
future use case demands it (e.g. untrusted or multi-tenant operation).

## Consequences

**Positive**
- The lifecycle is a plugin class, not control flow — satisfies the core motivation and
  Milestone 1 (parity flow + free-form flow as two subclasses of the workflow ABC).
- ADOPT and skip-review stop being special-cased modes; they are just different
  workflow subclasses (matches PARITY: ADOPT "implement as workflow", skip-review
  "support w/ separate workflow").
- The core shrinks to genuinely reusable plumbing; per-flow logic lives in one place.
- **Authoring is agent-assisted code, not config-schema design** — users describe the
  workflow they want and an agent writes the subclass; full Python expressivity is
  available for the imperative methods, with the ABC + type checking as guardrails.

**Negative — and to resolve in design**
- **The declarative/imperative line within the class still needs discipline.** The ABC
  fixes the *format* (Python subclass), but a workflow that buries its state machine
  inside imperative methods instead of the declarative members would recreate
  cloude-cade's hardcoding. The ABC should make the declarative members the obvious,
  required path.
- **Where per-repo workflows live** (global registered paths vs. shipped inside a repo)
  ties to ADR 0005 (repo-configurable images) and is deferred to ARCHITECTURE.md, though
  the path-based loading mechanism itself is decided above.
- **Residual trust risk.** Reviewing workflow code is the user's responsibility (decided
  above, not engine-enforced). This is acceptable for single-user operation but should be
  revisited for Milestone 5 (remote execution) and any future multi-tenant use.
- **Responsibility resolution** is now decided (2026-06-12, refined 2026-06-14): entering a
  state seeds its responsibilities onto the new history entry, all `PENDING` (a promise); the
  agent fulfils them one at a time (`MET`, or `FAILED` with a comment), mutating that entry;
  the workflow gates a later advance on all being resolved, without knowing workflow specifics.
  Fulfilling a promise is `Task` behavior (the workflow isn't consulted); advancing is the
  workflow's.
- **Cleanup composition** — workflow-specific teardown must compose predictably with the
  core's agnostic teardown and its confirmation/exit-code gating (PARITY §12).
- **Multiple concurrent workflows** — the dashboard, history, and turn-tracking must
  render uniformly across tasks running *different* workflows.

## Related

- GOALS.md — Milestone 1 (parity + free-form workflow) and the flexibility motivation.
- ADR 0001 — store persists state/history; the **state-machine enforcement** noted
  there is now *per-workflow* (the workflow defines legal transitions; the core enforces
  them at the store boundary).
- ADR 0002 — dashboard renders tasks across heterogeneous workflows.
- ADR 0003 — the `Plan` artifact is written by a workflow's plan-accepted hook.
- ADR 0005 — composable workflow/repo container images: a workflow contributes image
  layers via its provisioning extension point.
- ADR 0006 — the task service that loads workflows (path-based registration) and drives
  their lifecycle.
- The workflow interface's concrete interface and spec format belong in `docs/ARCHITECTURE.md`.
