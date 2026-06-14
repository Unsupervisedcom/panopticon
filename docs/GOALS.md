# Goals

panopticon is a ground-up rewrite of the
[cloude-cade](https://github.com/tildesrc/cloude-cade) prototype.

## Motivation

1. **Flexible workflows, not one hardcoded flow.** cloude-cade bakes in a single
   lifecycle (PLANNING → ITERATING → REVIEW → MERGING → COMPLETE). The rewrite exists to
   support *more flexible workflows* — the existing flow becomes one workflow among
   several, including a less rigid "free-form" one.
2. **Modularity for sustained development.** The prototype has proved the concept. The
   rewrite must modularize the system so it can be developed and extended over time —
   parts (storage, dashboard, artifacts, workflows, agent runner, execution backend) are
   replaceable behind explicit boundaries rather than entangled.

## Non-goals

- Not a faithful interface. We re-derive the design; we do not preserve cloude-cade's
  internal structure (heavy shell, hardcoded flow).
- Not locked to Claude. The system must accommodate other agent CLIs (see Milestone 3),
  so nothing assumes `claude` specifically.
- Not locked to one UI or one machine. The dashboard and the place work runs are both
  pluggable (Milestones 4–5), not assumptions baked into the core.

## Milestones

### Milestone 1 — Parity + a second workflow
- Reach parity with the existing cloude-cade workflow.
- **Plus** a second, "free-form" workflow — proving the workflow is configurable, not
  hardcoded. *(Implies: the lifecycle/state machine is data/config behind a workflow
  interface, not control flow in code.)*
- Support **separate API keys / secrets per repo** — each repository has its own
  secrets, and a task inherits the secrets of the repo it operates on. *(Implies: a
  repo/project entity owns secret references; tasks resolve secrets via their repo.)*

### Milestone 2 — Split planning and implementation agents
- Allow **planning with one agent and implementing with another**, so planning uses full
  reasoning power while implementation is delegated to an appropriately-sized reasoning
  level. *(Implies: agent/model selection is configurable per workflow stage.)*

### Milestone 3 — Other agent CLIs
- Support agent CLI tools **other than `claude`**. *(Implies: an agent-runner interface that
  abstracts over CLI tools.)*

### Milestone 4 — Web-hosted dashboard
- Support a **web-hosted view** of the dashboard. *(Directly exercises the substitutable
  presentation interface from ADR 0002.)*

### Milestone 5 — Remote execution
- Allow tasks to **spawn on external machines**. *(Implies: the execution/compute
  backend is an interface — local Docker now, remote machines later.)*

## What this implies for the design

The motivation (flexibility + modularity) and the milestones converge on the same
interface-and-adapters spine already started in the ADRs: a UI-agnostic, backend-agnostic core that
depends on **interfaces**, with replaceable adapters. The milestones add interfaces beyond the
three already decided:

| Concern | Interface | Adapter (now) | Driven by |
|---|---|---|---|
| Structured state | store | SQLite | ADR 0001 |
| Dashboard | presentation | Textual TUI | ADR 0002, Milestone 4 |
| Artifacts (plan, notes) | artifact store | local filesystem | ADR 0003 |
| **Workflow / lifecycle** | workflow | cloude-cade flow + free-form | Milestone 1 |
| **Agent invocation** | agent runner | `claude` CLI | Milestones 2–3 |
| **Where work runs** | execution backend | local Docker + tmux | Milestone 5 |

The last three are candidates for their own ADRs as those milestones are approached.

## Open questions

_None outstanding._
