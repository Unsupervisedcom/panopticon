# TODO — Guiding Artifacts for the panopticon Rewrite

panopticon is a ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype. The prototype is the **spec-by-example**; the artifacts below capture
*intent and target shape* so the rewrite re-derives the design rather than
line-by-line porting the prototype's Python/Shell.

## Decisions to lock first
These two forks shape every artifact below.

- [x] **Implementation language** — Python to start, with a substitutable dashboard
      behind a presentation port. See [`docs/decisions/0002-implementation-language.md`](docs/decisions/0002-implementation-language.md).
- [x] **Task-store format** — structured DB (SQLite first, backend-agnostic) for
      structured state; file-backed artifacts (plan, notes) served via MCP / filesystem /
      dashboard. See [`0001-task-store-format.md`](docs/decisions/0001-task-store-format.md)
      and [`0003-task-artifacts.md`](docs/decisions/0003-task-artifacts.md).

## Artifacts to create (priority order)

- [x] **`docs/GOALS.md`** — motivation (flexible workflows + modularity), non-goals,
      and 5 milestones. See [`docs/GOALS.md`](docs/GOALS.md).

- [x] **`docs/PARITY.md`** — feature inventory mined from the prototype, grouped into 14
      areas with an empty K/C/D column to fill. See [`docs/PARITY.md`](docs/PARITY.md).
  - [x] **Charlie categorized** every row keep / change / drop (all 14 sections).

- [x] **`docs/ARCHITECTURE.md`** — target design: 3-role topology, determinism invariant,
      ports/adapters, workflow model, data model, lifecycle walkthrough, module layout,
      milestone map. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

- **`docs/decisions/`** — ADRs
  - [x] 0001 — task store: structured DB, backend-agnostic
  - [x] 0002 — implementation language: Python, substitutable dashboard
  - [x] 0003 — task artifacts: file-backed, served via MCP / fs / dashboard
  - [x] 0004 — workflow abstraction: Python ABC, declarative members + imperative methods
  - [x] 0005 — composable workflow/repo container images
  - [x] 0006 — the task service (core runtime; sole DB authority; loads workflows)
  - [x] 0007 — per-repo secrets: runtime env vars + credential mounts (no secret-store port)
  - [x] 0008 — execution & session topology: separate services via Compose (task service /
        session service / terminal controller; 3 deterministic roles)

- [x] **`docs/ROADMAP.md`** — vertical slices. M1 decomposed into 7 slices (slice 1 =
      contracts + walking skeleton); M2–M5 as coarser entries; open-question → slice map.
      See [`docs/ROADMAP.md`](docs/ROADMAP.md).

- **`CLAUDE.md`** — operating manual for the agent — **deferred**; built incrementally.
  Each roadmap slice updates it (see ROADMAP "Definition of done — every slice"). Not
  written up front, to avoid documenting commands/conventions that don't exist yet.

- **Golden state-machine test / acceptance harness** — folded into the roadmap: created in
  Slice 1 (engine), expanded in Slice 4 (parity transitions), extended by every slice.
