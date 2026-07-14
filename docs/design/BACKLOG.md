# Backlog

Things found along the way that we are **not** dealing with right away. This keeps the
ROADMAP focused on planned work and the ADRs focused on decisions, while making sure
incidental findings aren't lost.

**How to use:** when you discover something out of scope for the current task, add a line
here instead of expanding the task. Each item: a short title, one line of context, where it
was found, and a rough priority (P1 = soon / P2 = eventually / P3 = nice-to-have). Pull
items into the ROADMAP when they're ready to be scheduled; delete them when done or obsolete.

Larger deferred concerns already tracked live in the ROADMAP's open-question → slice map and
in the ADRs; this file is for the smaller stuff that doesn't have a home there yet.

## Cleanups / tech debt

- [ ] **Per-task secret authorizes task mutations** — the task service trusts any caller: a request
  names a `task_id` and mutates it, over REST or MCP. Now that in-container agents reach the shared
  MCP server (operations as tools, `task_id` injected into the rendered skills), nothing stops one
  task's container from passing another task's id. Issue a **per-task secret** at create/spawn,
  inject it into the container (env + the rendered MCP config), and require it on the
  state-mutating tools/endpoints — scoping a container to *its own* task. Pairs with disabling MCP
  DNS-rebinding protection (`taskservice/mcp.py`) and runner inter-process auth (ADR 0008/M5).
  _(MCP-in-container, P1.)_

- [ ] **Short-circuit already-handled tasks in the host loop** — `HostDaemon.tick` calls
  `spawn_one` + `provision` on **every** task each pass and relies on each sub-step's self-gating
  (spawn skips claimed/terminal; provision skips unslugged/already-branched). That's correct but
  costs a `get_repo`/REST round-trip per already-spawned/provisioned task every pass. Pre-filter to
  spawnable + unprovisioned tasks (or track handled ids across passes) before the per-task work.
  _(Self-host host daemon, P2; pairs with the server-side spawnable-tasks query below.)_
- [ ] **Move the spawnable-tasks query into the task service** — the session service's spawn loop
  filters `list_tasks()` client-side for unclaimed + non-terminal tasks (`spawner.spawnable_tasks`),
  matching the built-in terminal labels (`core.state.TERMINAL_LABELS`). A server-side endpoint —
  "tasks needing a runner" — would let the task service compute terminal-ness from each workflow's
  graph (so custom workflows' terminal states are honored) and scope to a runner, instead of the
  runner hardcoding labels. _(Self-host spawn loop, P2; pairs with runner registration, ADR 0008/M5.)_
- [ ] **Starlette/httpx TestClient deprecation warning** — tests emit "Using `httpx` with
  `starlette.testclient` is deprecated; install `httpx2` instead." Harmless, but noisy.
  Pin/upgrade once the ecosystem settles. _(Slice 1, P3)_
- [ ] **CI doesn't type-check `tests/`** — `mypy -p panopticon` covers the package only.
  Consider adding `mypy tests` (needs path/namespace config). _(Slice 1, P3)_
- [ ] **`advanced_by` is declared but not engine-enforced** — a state's `advanced_by`
  (`USER`/`AGENT`) is queryable metadata; the engine doesn't yet use it to decide who
  may trigger a transition, nor is there a per-*transition* auto-advance flag. Wire it when
  the agent runtime needs it (around the parity workflow, Slice 4). _(Slice 1, P2)_
- [x] **No schema migrations** — ~~the SQLAlchemy adapter creates tables with
  `metadata.create_all`; there's no versioning/upgrade path.~~ Done: Alembic now owns versioned
  evolution (`migrations/`, `alembic.ini`, `make migrate`). `create_all` remains the zero-config
  bootstrap for fresh/in-memory DBs; `tests/test_migrations.py` guards the two against drift.
  _(Slice 1, P2)_
- [ ] **Factor the polling loops** — coordination is moving to pull/poll: the session service
  observing slug-set + assigned work (ADR 0010), the agent waiting for "provisioned", dashboard
  refresh, the container heartbeat. Before they multiply, see whether they can share one
  observe/poll abstraction — a single place to set intervals/backoff, batch reads, and later swap
  poll → long-poll/SSE uniformly. _(ADR 0010, P3)_

## Deferred features (not yet scheduled, or scheduled but flagged here)

- [ ] **Runnable task-service entrypoint** — there's no `uvicorn`-runnable server +
  config (host/interface, DB path, artifact root, workflow path). Tests drive `create_app`
  in-process. Needed before Slice 2/3 can run for real. _(Slice 1, P1)_
- [ ] **Workflow path-based registration** — workflows are injected as a dict today; ADR
  0004/0006 specify loading from a registered path at startup (ROADMAP Slice 7). _(Slice 1, P2)_
- [ ] **Registrations are in-memory** — lost on task-service restart; no reconciliation with
  live containers on reconnect (relates to ADR 0008 failure-handling). _(Slice 1, P2)_
- [ ] **Slug-addressable artifacts** — once a task has a `slug`, both
  `tasks/{task_id}/artifacts/{name}` and `tasks/{slug}/artifacts/{name}` should resolve to the
  same artifact (slug as an alias for the id on the artifact routes). _(Slice 1, P3)_
- [ ] **Wire provisioning into the lifecycle** — the core git ops (`core/git.py`), the
  `Workflow.provision` seam, and `TaskService.provision_task` (slug-gated) exist (Slice 4), but
  nothing **triggers** them: `provision_task` should run automatically once the slug is set, and
  it needs **repo clone-path management** (where each repo's local clone lives) plus a
  `worktrees_root` config — both passed in by the caller today. Wire this through the runner/
  spawn flow per ARCHITECTURE §9 (slug → worktree → provisioning). _(Slice 4, P2)_
- [ ] **Declarative image composition vs. hand-rolled Dockerfile fragments** —
  `sessionservice/images.py` composes the base→workflow→repo image by string-concatenating
  Dockerfile fragments and `docker build`ing them. Evaluate purpose-built tooling for
  layer composition/ordering, caching, and reproducibility before this scales: Cloud Native
  Buildpacks (`pack`/Paketo), Chainguard `apko` (declarative package-list images), or
  `docker buildx bake` (target inheritance, which maps cleanly onto base→workflow→repo). Not
  needed now; the fragment approach is the minimal thing that works. _(Slice 6, P3)_

## Tracked elsewhere (pointers, do not duplicate)

- Artifact concurrency / drift detection → ADR 0003 / ROADMAP open-questions.
- At-rest secret protection & remote delivery → ADR 0007 / M5.
- Runner registration/discovery, inter-process auth, restart reconciliation → ADR 0008 / M5.
- Image matrix, registry storage, rebuild triggers, layer order → ADR 0005 / M5.
