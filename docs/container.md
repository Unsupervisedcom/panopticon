# The lifecycle of a task container

Every task in panopticon runs in its own **container** — a throwaway Docker container with a
`claude` agent inside it, working on a private clone of the repo. This doc explains that
container's life from the operator's chair: how one comes up, the statuses you watch on the
dashboard, what each means, and how a container recovers or is torn down. It's about *observable
behaviour*, not internals — for the design rationale, follow the ADR links.

If you just want to give a repo its `claude` token, see
[container authentication](container-auth.md). For the whole-system picture, see
[the architecture doc](design/ARCHITECTURE.md).

## One task, one container, one session

A task's work happens in exactly one place: its container. Nothing else in panopticon runs an
LLM — the task service (control plane) and the session service (runner) are deterministic host
processes that never call `claude`. That's the **determinism invariant** (ADR 0008): the
container is the *only* LLM-bearing component, so everything the agent does is scoped to it.

Concretely, each running task has:

- a **container** named `panopticon-<task-id>`, spawned by the session service on the host's
  Docker daemon;
- a **tmux session** (`panopticon-<task-id>`) whose pane runs the agent — this is what you attach
  to with `t` from the dashboard;
- a **per-task clone** of the repo, mounted read-write at `/workspace`, on the task's own branch.

The **session service** (the per-host runner) owns the container's whole lifecycle — it claims
the task, builds the image, starts the container, and later heals or cleans it up. The **task
service** only ever *records* and *displays* what the runner reports; it spawns nothing itself.

```
   queued
     │  a runner claims the task (ADR 0008)
     ▼
   claiming → preparing → building → starting → awaiting
     │  clone      │  compose+build   │  docker run   │  entrypoint
     │  /workspace │  the image       │  + tmux       │  connects & registers
     ▼
    live ──────────────────────────────► (task reaches a terminal state)
     │  agent working; you can attach          │
     │                                         ▼
     ▼  container vanishes                   cleanup: stop container,
    down ──respawn (self-heal, or `R`)──►    release claim, remove /workspace
```

## The status you see on the dashboard

The dashboard shows one **container status** per task — a single word the task service computes
by folding three signals together: the spawn *phase* the runner is reporting, whether the
container has an open **registration** (its live connection), and whether the **runner** itself
is still connected. First match wins, so a status higher in this table always overrides a lower
one.

| Status | What it means | What you do |
|---|---|---|
| `queued` | Non-terminal but unclaimed — no runner has picked it up yet. | Wait; if it sticks, check a runner is actually running (`make start`). |
| `claiming` | A runner just claimed it; the spawn is about to start. | Nothing — transient. |
| `preparing` | Readying the per-task clone / workspace. | Nothing — transient. |
| `building` | Composing and `docker build`-ing the image. **The slow step on a first run** for a repo (later runs hit Docker's cache). | Wait; first build of a repo image can take minutes. |
| `starting` | `docker run` and the tmux session are coming up. | Nothing — transient. |
| `awaiting` | Container and tmux are up; waiting for the agent to open its `/live` connection. | Nothing — transient; if it lingers, see *When it goes wrong*. |
| `live` | A container registration is open — the agent is running and reachable. | Attach with `t` to watch or steer. |
| `down` | Claimed, the runner is alive, but the container is gone and unregistered. It came up and vanished, or never reported. | Respawn from the dashboard with `R` (the runner also self-heals — see below). |
| `failed` | A spawn step raised an error (hover / inspect for the detail). | Read the detail; fix the cause (bad image layer, missing secret) and respawn. |
| `disconnected` | Claimed by a runner that's **no longer connected** to the task service. | Bring that runner's host back, or the task stays stranded until its claim is released. |
| `–` | Terminal task (COMPLETE / DROPPED) — no container concept. | Nothing. |

The dashboard only *displays* this status; it does no liveness guessing of its own. The five
middle statuses (`claiming`→`awaiting`) are the **spawn phases** the runner pushes as it works;
`queued`, `live`, `down`, and `disconnected` are *derived* by the task service from
registration + runner liveness, so the runner never invents them. (See
`compose_container_status` in `core/models.py` for the exact precedence.)

## Coming up: the spawn sequence

When a new task appears, the per-host session service brings its container up in five steps,
reporting each as a status above.

1. **Claim** (`claiming`). A runner **claims** the unclaimed task first — a compare-and-set that
   409s if another host got there first (ADR 0008). This is the spawn gate: exactly one host
   ever runs a given task, even with several runners watching the same task service.

2. **Prepare** (`preparing`). The runner makes the task's **workspace**: a
   `git clone --local` of a per-repo cache clone into a task-private directory, mounted
   read-write at `/workspace` (ADR 0011). The clone is self-contained (hard-linked objects, so
   it's cheap), and its `origin` is pointed at the real forge rather than the local cache.

3. **Build** (`building`). The runner composes the task's image from three layers — **base →
   workflow → repo** (ADR 0005) — and `docker build`s it. The base layer is Python + git + the
   `claude` CLI; the workflow layer adds workflow tools (e.g. `gh` for the GitHub workflows); the
   repo layer adds the repo's own toolchain. The image is tagged per `(workflow, repo)`, so once
   built it's cached — only the **first** task for a repo pays the full build cost.

4. **Start** (`starting`). The runner does `docker run --detach` (injecting the repo's secrets
   via `--env-file`, mounting `/workspace`, and a small config volume that persists the agent's
   history across respawns), then creates the tmux session whose pane execs the agent inside the
   container.

5. **Await** (`awaiting`). The container's entrypoint starts up. It first **remaps** its baked
   `panopticon` user to the invoking host's uid/gid (`PANOPTICON_PUID`/`PGID`) and drops
   privileges via `gosu` — so files the agent writes under `/workspace` are owned by *you* on the
   host, not root. Then it **connects to the task service and holds the connection open**,
   registering that this container is working on this task.

Once that registration is open, the status flips to **`live`** and the agent is off.

## Provisioning: naming the task gives it a branch

A fresh task has no slug, so it has no branch — it starts on whatever the clone checked out. Early
in its first turn the agent (nudged by the `provision` skill) picks a short **slug** and sets it.
The session service, watching the task over its pull loop, sees the slug land and **branches the
workspace**: `git checkout -b panopticon/<slug>` on the per-task clone, with `origin` already
pointed at the forge (ADR 0010 / ADR 0011).

The host git happens on the runner — where the container actually is — so it stays correct even
when the runner is remote. The task service only *records* the result (the branch name and clone
path); it touches no filesystem. From then on the task is **provisioned** and the agent commits on
its own `panopticon/<slug>` branch.

## While it runs

- **Liveness is connection-based, not a heartbeat.** The container holds one long-lived `/live`
  connection to the task service. While it's open the task reads `live`; if it drops (crash,
  kill, network blip) the registration clears and the task stops being `live`. The container
  reconnects with backoff across transient blips.
- **The agent.** In the tmux pane, the launcher wires everything the agent needs and then execs
  `claude`: it renders the workflow's **skills** and **operations** as slash-commands, points the
  agent's **MCP** client at the task service (so it can read/write artifacts and drive its own
  state), and puts the workflow's state-machine overview in the system prompt.
- **The task memo is pre-filled.** On a task's first spawn the runner pastes the task's
  description into the agent's input box (unsent), so you see the ask waiting when you attach.
- **Turn and blocked.** As the agent and user hand the work back and forth, the task's **turn**
  flips (`agent` ↔ `user`) via in-container hooks; a separate **blocked** marker is a deliberate
  "waiting on something" flag the agent sets. Both are shown on the dashboard.
- **Auth.** The agent authenticates from a `CLAUDE_CODE_OAUTH_TOKEN` injected from the repo's
  env-file — see [container authentication](container-auth.md).

## When it goes wrong

A container can disappear out from under a live task — an OOM kill, a host reboot, a `docker rm`.
The system distinguishes a few cases:

- **`down`** — the container is gone but its runner is alive. The runner's **reconcile** pass
  notices the container has vanished and clears the stale spawn phase, so the task composes to
  `down` rather than lying at `awaiting`.
- **Self-heal.** The runner also **heals** orphans automatically: a task it still owns whose tmux
  session is gone gets respawned through the same idempotent spawn path (the agent resumes from
  its persisted history). A **crash-loop cap** (a handful of respawns within a short window)
  stops a hopelessly failing task from respawning forever — past the cap it's left `down` for you
  to look at.
- **Manual respawn.** You can always respawn a `down` task yourself from the dashboard with
  **`R`**. This is also how you pick up a changed secret or env-file — respawn to restart the
  container with the new values.
- **`disconnected`** — the *runner* is gone, not just the container. The task is stuck claimed by
  an absent host. Bring that host back (its runner reclaims and heals), or release the claim so
  another runner can take over.
- **`failed`** — a spawn step raised. The status carries a detail string (e.g. a broken image
  layer or a missing secret). Fix the underlying cause, then respawn.

## Teardown

When a task reaches a terminal state (COMPLETE or DROPPED), its container is no longer needed. The
runner's **cleanup** pass stops the container, releases the claim, and removes the per-task
`/workspace` clone. If a delete is blocked (e.g. by a file left root-owned by a nested build), it
escalates — an as-root sweep, then quarantining the directory aside — so a stuck workspace never
wedges the runner. After cleanup the task shows `–`: no container, nothing to attach to.

## See also

- [Container authentication](container-auth.md) — giving a repo its `claude` token.
- [Architecture](design/ARCHITECTURE.md) — the whole system; §3 the determinism invariant, §9
  the end-to-end task lifecycle.
- ADR [0005](design/decisions/0005-composable-images.md) — composable base → workflow → repo
  images.
- ADR [0008](design/decisions/0008-execution-session-topology.md) — host processes, the claim
  gate, and container topology.
- ADR [0011](design/decisions/0011-provisioning-per-task-clone.md) — the per-task clone and
  slug-named branch.
