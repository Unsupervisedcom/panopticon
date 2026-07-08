# Roadmap

Vertical slices toward the milestones in `docs/GOALS.md`, grounded in
`docs/ARCHITECTURE.md` and the ADRs. Each slice is **independently shippable and
verifiable** — a thin path through multiple components, not a horizontal layer.

Two principles guide the ordering:

- **Walking skeleton first.** Slice 1 locks the contracts and proves the thinnest
  end-to-end path; later slices thicken each part behind those contracts.
- **Resolve open questions just-in-time.** The ADRs deliberately deferred many questions
  (mostly M5/hardening). Each is pulled into the slice that first needs it — not up front.
  See the map at the end.

**Definition of done — every slice also:**
- **updates `CLAUDE.md`** (deferred as a standalone artifact) with the build/test/run
  commands, conventions, and module pointers that slice introduces — so the operating manual
  grows with the code instead of being written up front and drifting;
- extends the **golden state-machine harness** / tests to cover what it added;
- keeps the ADRs and `docs/ARCHITECTURE.md` current if the slice changes a decision;
- **logs incidental findings to [`docs/BACKLOG.md`](BACKLOG.md)** rather than expanding the
  slice's scope to handle them.

Milestone 1 (parity + a free-form workflow + per-repo secrets) is decomposed into slices
1–8. Milestones 2–5 are coarser entries, since each is largely "fill in an already-designed
interface."

---

## Milestone 1 — parity flow + free-form flow

### Slice 1 — Contracts + walking skeleton

**Goal:** lock the four interface contracts and prove them end-to-end with stubs, so all
later work can proceed in parallel against stable seams.

**Delivers — the four contracts:**
1. **Workflow ABC** (`core/`) — declarative members (states, transitions, fg/bg
   classification, transition policy, `definition_of_done`, `skills()` catalogue) and the
   deterministic-method signatures. Plus a trivial **"free-form seed" workflow** (1–2 states,
   no gates) implementing it.
2. **DB schema + store interface** — `repo`, `task` (internal id, optional slug), `history`
   tables; SQLite adapter; migrations. Transition enforcement lives at this boundary.
3. **Task service API contract** — the REST endpoints + MCP tools/resources, defined as a
   schema with minimal implementations (create/query task, request transition, set slug,
   read/write artifact).
4. **Container liveness/registration protocol** — how a container connects, registers
   "working on task X", and stays connected.

Plus a **stubbed runner** and **fake container client** that drive the seed workflow through
the real task service (no Docker yet), and the **Workflow state machine** (transitions, turn, responsibilities) with its
**golden test harness** (the durable parity spec).

**Acceptance:**
- A CLI/test creates a task → task service persists it → fake container registers (liveness)
  → sets a slug → requests a transition the workflow accepts → history reflects it.
- Workflow ABC + store + API are type-checked and covered by contract tests.
- Determinism holds: nothing in `core`/`taskservice` imports an LLM.

**Resolves (JIT):** container→host-service addressing (contract only); minimal runner
registration.

---

### Slice 2 — Real execution (runner + containers + tmux)

**Goal:** replace the stubs with a real runner host process that spawns real task containers.

**Delivers:** the session-service runner (host process) spawning a task container on the host
Docker daemon; the **host tmux** session (one per task); the real **container entrypoint**
(connect/register/liveness, slug hook); a minimal composed image (base layer only, ADR 0005).
The in-container agent step ships as a **stay-alive placeholder** — the real `claude` agent
lands in Slice 6.

**Acceptance:** the runner launches a real container that registers with the task service;
`tmux attach` reaches it; killing the container is reflected as lost liveness.

**Depends on:** Slice 1. **Resolves (JIT):** process supervision (minimal: terminal
controller starts the daemons); container→host-service addressing (real).

---

### Slice 3 — Terminal controller + dashboard

**Goal:** the operator surface.

**Delivers:** the `panopticon` CLI; the Textual dashboard (presentation adapter, ADR 0002) as
a REST client — task list, state/turn/history (read first); then **`t` → `tmux attach`** and
back; then **input** (capture idea, promote, drive transitions) over REST (PARITY §5).

**Acceptance:** the dashboard reflects live task state; `t` switches into a task's tmux and
rejoins on detach; a task can be created and advanced from the UI.

**Depends on:** Slices 1–2.

---

### Slice 4 — Parity workflow (core lifecycle)

**Goal:** the cloude-cade lifecycle as a workflow class — minus remote forge.

**Delivers:** the parity workflow (`PLANNING→ITERATING→REVIEW→MERGING→COMPLETE`/`DROPPED`),
its per-state responsibilities, fg/bg classification, transition policy; core
operations (`advance`, `drop`) — going back to coding is a free move (`set_state`), not a named
operation; **local git** (branch/worktree) as core ops;
the **plan artifact** (ADR 0003) + plan-accepted hook; turn-flip hooks.

**Acceptance:** a task runs the full parity lifecycle end-to-end (without forge skills),
gated by responsibilities, with the turn flipping correctly; the golden harness covers every
legal/illegal transition.

**Depends on:** Slices 1–3. **Resolves (JIT):** artifact concurrency (start simple:
re-read-from-disk).

---

### Slice 5 — Per-repo secrets

**Goal:** the secrets model (a prerequisite for forge skills, which need `GH_TOKEN`).

**Delivers:** the repo entity (ADR 0001/0007); per-repo **env config** + optional **creds
mount**; runner injection at launch scoped to the task's repo; the generalized interactive
**`login`** flow.

**Acceptance:** two repos with distinct secrets; a task receives only its own repo's env +
creds; secrets never appear in the DB, artifacts, or image layers.

**Depends on:** Slices 1–2.

---

### Slice 6 — Claude integration (real agent + skills)

**Goal:** stand up the **real in-container `claude` agent** — replacing Slice 2's stay-alive
placeholder — and the workflow's in-container **skills** it runs (per the determinism boundary:
the agent and its skills are the only LLM-bearing code, ADR 0008).

**Delivers:** the real agent runtime in the task container — `claude` doing the work, reading
and writing artifacts (plan/notes) and calling back over REST/MCP to drive the lifecycle
(core operations / `set_state`) and resolve responsibilities; plus the **workflow-contributed
skills** layered on the core operations — for the parity workflow, the forge behaviors: `gh`
PR creation, `babysit-ci` (watch/diagnose/fix loop, retry/budget), `babysit-merge`
(merge-queue shepherding), and ADOPT-style checkout. Skills are exposed only by workflows that
define them (a forge-less workflow gets none). Plus the **claude-specific hooks** that wire the
contracts defined in Slice 4: the turn-flip hooks (stop → `turn=user`, user-prompt →
`turn=agent`, with `:blocked:` preserved) and the prefill prompt.

**Acceptance:** a parity task is driven end-to-end by a real `claude` agent — it plans,
implements, advances through the lifecycle, opens a PR, `babysit-ci` reacts to CI and
`babysit-merge` lands it, reaching COMPLETE; a forge-less workflow exposes none of the forge
skills. (Real-agent/forge tests are `skipif`-gated; no LLM in CI.)

**Depends on:** Slices 4–5.

---

### Slice 7 — Task provisioning (worktrees, session-service-owned)

**Goal:** wire provisioning into the lifecycle (ADR 0010) so a task actually gets a working tree
to operate in — the prerequisite for real end-to-end execution.

**Delivers:** the **session-service daemon** fleshed out from today's one-shot spawn primitive
into a long-lived per-host loop that **observes its tasks over REST (pull)**. On a task acquiring
a slug it creates the slug-named **worktree/branch** on the host where the container runs
(`core/git.py`), repoints the container's working path from the initial **read-only checkout** to
the worktree (the agent `cd`s in), and runs the active workflow's host-side provisioning. The
task service only **records** the result (branch/worktree refs) — it does no host filesystem
work, so this stays correct when it's remote (ADR 0009). Plus per-repo **clone-cache** management
on the host.

**Acceptance:** a task spawned with no slug starts on a read-only checkout; once the agent sets
its slug, a worktree named `panopticon/<slug>` appears, the agent works in it, and parity's PR is
opened against it — with the task service never touching the host FS. (`skipif`-gated for real
docker/git; no LLM in CI.)

**Depends on:** Slices 2, 4, 6. **Resolves (JIT):** "wire provisioning into the lifecycle"
(backlog); slug → worktree → provisioning, observed via pull (ADR 0010).

---

### Slice 8 — Free-form workflow + multi-workflow proof

**Goal:** prove the lifecycle is genuinely configurable (the Milestone 1 thesis).

**Delivers:** the free-form workflow finalized; the container command surface and dashboard
**enumerate skills/operations from the active workflow** (not a global menu, ADR 0004);
path-based registration of multiple workflows.

**Acceptance:** parity and free-form tasks run **concurrently**; their available skills differ
per workflow; adding a workflow class on a registered path makes it selectable with no core
change.

**Depends on:** Slices 1–7.

---

## Milestone 2 — split planning and implementing agents

Largely new use of designed seams: an **agent-runner interface** selecting model/role per stage,
and **multiple containers per task** (planner + implementer) under one tmux session — the
exact case tmux-outside-the-agent-container was designed for (ADR 0008). Touches the workflow
`skills()` split and container invocation.

## Milestone 3 — other agent CLIs

Add **agent-runner adapters** beyond `claude` and **base-image variants** (ADR 0005). The
`login`/secrets model already generalizes (Slice 5).

## Milestone 4 — web-hosted dashboard

A **second presentation adapter** consuming the existing task-service REST API (ADR 0002).
The API was made network-capable from the start (ADR 0006), so this adds a client, not core
changes. **Resolves:** inter-process auth/transport for a networked client.

## Milestone 5 — remote execution

Run a **runner host process per machine** pointed at the central task service (ADR 0008).
The mechanics are decided in **ADR 0013** and built in slices:

- **M5.1 (trusted network, first slice)** — add `host` to runner registration; use
  `ssh -t <host> tmux attach` in the terminal supervisor for remote tasks. The runner already
  reaches the task service via `--service-url`; containers reach it via
  `--container-service-url`; images build locally on each host; secrets are copied at the
  same path. This slice **resolves** (for the trusted-network case): container → task-service
  callback, remote tmux attach, runner host tracking, failure/restart reconciliation, and
  artifact reach (already solved via MCP/REST).
- **Later M5 slices** — inter-process auth (bearer tokens, per-task MCP scoping), image
  registry (push/pull), `--secrets-root` path remapping, and at-rest secret protection.

---

## Open-question → slice map

| Open question (source ADR) | Resolved in |
|---|---|
| Container → host-service addressing (0008) | Slice 1 (contract) → 2 (real) |
| Minimal runner registration (0008) | Slice 1 / 2 |
| Process supervision of host daemons (0008) | Slice 2 (minimal) |
| Artifact concurrency / drift (0003) | Slice 4 (simple) |
| Declarative/imperative discipline in workflows (0004) | Slice 1 (ABC shape) + 4 |
| Image layer order / rebuild triggers (0005) | Slice 2 (base) → M3 (layers) |
| Wire provisioning into the lifecycle; slug → worktree, observed via pull (0010) | Slice 7 |
| Workflow skill enumeration per workflow (0004) | Slice 8 |
| Inter-process auth & transport (0006/0008) | M4 (networked) / M5 later slice (ADR 0013 §9) |
| Runner registration/discovery, full (0008) | M5.1 (host field, ADR 0013 §6) |
| Failure/restart reconciliation (0008) | M5.1 (existing mechanisms sufficient, ADR 0013 §8) |
| Remote tmux attach (0009) | M5.1 (ssh prefix on attach_command, ADR 0013 §7) |
| Container → task-service callback, remote (0008) | M5.1 (--container-service-url, ADR 0013 §2) |
| At-rest secret protection / remote delivery (0007) | Slice 5 (local) / M5 later slice (ADR 0013 §9) |
| Image matrix & registry storage (0005) | M5 later slice (ADR 0013 §9) |
