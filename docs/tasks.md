# Tasks: the lifecycle and the properties that define one

A **task** is Panopticon's unit of work — one change an agent takes from "here's what I
want" to "it's landed." Everything the system does is in service of tasks: a runner spawns
a container *for* a task, the dashboard lists *tasks*, a workflow is the lifecycle a *task*
moves through. This page explains what a task is made of and how it travels from creation
to done.

It's the companion to [the workflow catalog](workflows/README.md): that page is about
*choosing* how a task runs; this one is about the *task itself* — the object every
workflow drives. For the system's roles and topology, see [the overview](overview.md).

## What a task is

A task is a record owned entirely by the **task service** — the control plane, and the
single writer of task state. Its identity is an internal `id` (generated at creation,
never changes); everything else about it is a property the workflow and the agent evolve
over the task's life. One task runs exactly one **workflow**, against one **repo**, and
lives on the dashboard until it reaches a terminal state.

A task's *state* is a plain, inspectable record: the agent proposes changes to it through
a narrow, gated interface (see [How a task is acted on](#how-a-task-is-acted-on)), never
writing task state directly. That the control plane runs no LLM is a system-wide
invariant — see [the overview](overview.md).

## Essential properties

These are the fields that define a task (from `core/models.py`). Most are **stored**; a few
are **computed** from the others and are marked as such.

### Identity and intent

| Property | Meaning |
|---|---|
| `id` | Internal identity, generated at creation, immutable. The one thing that never changes. |
| `slug` | A short human label the **agent** sets later (e.g. `docs-task-lifecycle`). Names the task's branch; `None` until set. |
| `memo` | A brief one-line reminder of intent, collected when the task is created and shown in the dashboard summary. Not the full description — that's the plan artifact. |
| `initial_prompt` | Optional text prefilled (unsent) into the agent's input box on first spawn; takes precedence over `memo` for that purpose. |
| `workflow` | The lifecycle this task runs (e.g. `github-self-reviewed`). Fixed at creation. |
| `repo_id` | The [repository](repos.md) the task operates on. |

### Position in the lifecycle

| Property | Meaning |
|---|---|
| `state` | The current state label (e.g. `PLANNING`, `ITERATING`). Comes from the workflow. |
| `turn` | Who holds the ball right now — `user` or `agent`. Flips back and forth *within* a state. |
| `blocked` | A deliberate "waiting on something" marker the agent sets. **Orthogonal to `turn`** — it survives turn flips and is cleared only explicitly. |
| `history` | Append-only log of every state entry, each carrying the responsibilities promised on entering that state and how they were resolved. |
| `outstanding_responsibilities` | *(computed)* The promises on the current state still unresolved. Empty means the task may advance. |

### Provisioning (the git refs)

| Property | Meaning |
|---|---|
| `branch` | The slug-named branch `panopticon/<slug>` the session service creates once the slug is set. `None` until provisioned. |
| `clone` | Path of the per-task clone the task works in, on the host where its container runs. `None` until provisioned. |
| `provisioned` | *(computed)* `True` once the branch is recorded (`branch is not None`). |

See [Provisioning](#provisioning) below.

### Execution and ownership

| Property | Meaning |
|---|---|
| `claimed_by` | The runner (session service) that has **claimed** the task — the spawn gate, so exactly one host runs it. `None` if unclaimed. |
| `container_status` | *(computed)* The single status the dashboard shows, folding spawn progress with liveness. See [Container status](#container-status). |
| `runner_host` | *(computed, at query time)* The host of the claiming runner, used to attach to remote sessions. |
| `starting_model` | The model the agent starts with (e.g. `opus`), seeded from the workflow's default. |

### Bookkeeping and relationships

| Property | Meaning |
|---|---|
| `url` | An external URL for the task — usually its pull request. Set via the `set_url` tool; the dashboard's `p` hotkey opens it. |
| `token_estimate` | The agent's forecast of total cost-weighted tokens, set once during planning. |
| `tokens_used` | Cumulative cost-weighted tokens actually consumed (input-equivalent units: cache-reads ≈0.1×, output ≈5×), reported each turn. |
| `depends_on_task_ids` | Tasks that should reach a terminal state before this one begins. **Tracking only** — the state machine does not enforce it. |
| `governor_task_id` | The task that *governs* (oversees) this one, set by an orchestrator on the tasks it creates. `None` for ungoverned tasks. |
| `created_at` / `updated_at` | ISO-8601 timestamps: `created_at` stamped once, `updated_at` on every mutation. The state machine itself is clock-free — the task service passes timestamps in. |

## The lifecycle

A task's lifecycle is a **state machine**, and the workflow *is* that machine. States are
declared as classes (`State` for non-terminal, `TerminalState` for done), each carrying a
`label`, a `description`, the actor who holds the turn on entry, the actor who advances out,
and the agent's responsibilities for that state. The lifecycle is code — not control flow
baked into the engine.

### Who acts, and who advances

Two questions have the same two answers (`user` or `agent`), and they're **orthogonal**:

- **`turn_on_enter`** — who holds the turn when the task *enters* a state. The initial
  state always starts on the **user** (the agent needs a first instruction); most other
  states start on the agent (it acts, then hands back).
- **`advanced_by`** — who moves the task *out* of the state. The default is the **user**
  (you review and approve); a state the agent shepherds itself sets `advanced_by = agent`.

Within a state the live `turn` flips as work goes back and forth; `advanced_by` is about
crossing to the *next* state.

### Moving between states

Three ways a task changes state:

- **`advance`** — the happy path. A state with a single non-`DROPPED` transition derives an
  `advance` operation automatically; taking it is **gated on the state's responsibilities**
  all being resolved. This starts a new agentic turn, so an in-container agent skill invokes
  it (over MCP), never the dashboard.
- **`drop`** — the universal escape. Every non-terminal state can go straight to `DROPPED`
  (dashboard `x`), no gate. Nothing lands.
- **free move (`set_state`)** — moving a task to *any* state directly, off the declared
  graph **and bypassing the responsibility gate**. This is user authority (e.g. sending a
  task back from ITERATING to redo planning); being a transition, it runs through an agent
  skill, with the user directing.

### Responsibilities gate the happy path

Each state seeds its **responsibilities** onto the history entry when the task enters it,
all `PENDING` — promises the agent must keep. The agent resolves each one to `MET`, or
`FAILED` with a comment explaining why. A later `advance` is blocked until none are still
`PENDING`. Responsibilities are agent-only; user approval is expressed by advancing, not by
a responsibility.

### A concrete path

This repo's own tasks run [`github-self-reviewed`](workflows/github-self-reviewed.md):

```
PLANNING → ITERATING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

- **PLANNING** (starts on you, you advance) — the agent collects requirements and writes a
  `plan.md` artifact plus a token estimate; those are its responsibilities. You review the
  plan and advance when satisfied.
- **ITERATING** (agent acts, you advance) — the agent implements, tests, commits and pushes,
  opens the PR and records its URL. You self-review the change; telling the agent to proceed
  to merging *is* your approval.
- **MERGING** (agent acts, **agent advances**) — `advanced_by = agent`: the agent shepherds
  the PR through the merge queue and advances itself to `COMPLETE` once merged.
- **COMPLETE** — terminal; the work has landed.

Other workflows vary the states (a peer-reviewed one inserts a `REVIEW` gate; a spike has no
gates at all) but the machinery is identical — see the [workflow catalog](workflows/README.md)
for each one's states and responsibilities.

## Provisioning

A fresh task has **no slug and no branch** — the agent works in the per-task clone mounted
at `/workspace` on whatever branch it came up on. Once the agent understands the task well
enough to *name* it, it runs the universal **`provision`** skill: it picks a kebab-case slug
and calls `set_slug`. The **session service** then branches the clone to `panopticon/<slug>`
and points `origin` at the forge; the task service only **records** the resulting `branch`
and `clone` (it does no git itself, so this stays correct when the runner is remote). At that
point `provisioned` becomes `True`. This is why the agent names the task before committing
anything. For the host-side git mechanics — the per-task clone, the mount, and the branch —
see [the container doc](container.md).

## Container status

Whether a task has a live container is a separate axis from its workflow state:
`container_status` folds the runner's reported spawn progress together with container and
runner liveness into the one status the dashboard shows (queued, building, live, down, and
so on). The [container doc](container.md) owns the full status set and how it's composed.

## How a task is acted on

The agent never writes task state directly — it goes through a narrow, deterministic
surface the task service exposes over MCP (and REST):

- **Operations** — `advance` and `drop`, the gated verbs of the state graph. The agent
  invokes `advance` through a skill; the dashboard drives only `drop`.
- **Artifacts** — the task's own documents (the plan, notes), file-backed and addressable
  at `panopticon://tasks/{id}/artifacts/{name}`. The plan lives here, not in the repo;
  read it from the dashboard with `a`.
- **Skills** — agent-driven procedures exposed in the container. Every task has the
  universal **`provision`** skill; a workflow adds its own (the GitHub workflows add
  `open-pr`, `babysit-ci`, `babysit-merge`).
- **Tools** — MCP tools like `set_slug`, `set_url`, `set_token_estimate`,
  `set_blocked`, and `resolve_responsibility` that record specific facts on the task.

Because all of this is mediated by the control plane, a task's record is always a
faithful, deterministic account of where the work stands — no matter what the agent is
doing inside the container.

## See also

- [Workflows: choosing how a task runs](workflows/README.md) — the catalog, and each
  workflow's states and responsibilities.
- [The overview](overview.md) — the system's roles and topology.
- [The container doc](container.md) — the container status set, and the host-side git a
  task is provisioned with.
- [Repos](repos.md) — the repositories tasks operate on.
