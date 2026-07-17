# 0008 — Execution & session topology: host processes + task containers

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

ADR 0006 made the task service the control-plane core. A remaining question was where
tmux/session management lives. "Managing tmux" is really **two separable concerns**:

1. **Session lifecycle** — creating/destroying the tmux session + container(s) a task runs
   in (the execution-backend work).
2. **Terminal attach/switching** — owning the user's TTY: running the dashboard and, on
   `t`, attaching their terminal to a task's session and back. Inherently local to wherever
   the human sits.

cloude-cade fused both (and fused them with the container). We resolved this in stages:

- **Split execution from control.** A separate **session service (runner)** owns session
  lifecycle and is a client of the task service — the standard control-plane /
  per-machine-runner pattern, and a natural fit for Milestone 5 (remote execution). (The
  alternative, the task service managing tmux directly, re-conflates control with execution
  and creates a TTY/daemon-liveness tension.)
- **tmux is not inside the agent container.** A task's "session" is a view *over* the
  task's container(s), not a thing inside one. Milestone 2 will give a single task separate
  planner and implementer containers, so tmux can't sensibly live inside "the" agent
  container. tmux runs at the **runner level**, one session per task, with panes that exec
  into whichever of the task's containers you want to watch.
- **Host processes, not containers, for our own services.** Because the runner bridges the
  user's terminal, host tmux, and container spawning — all host-coupled — it runs as a
  **host process** with a **host-level tmux server** (plain `tmux attach`, no cross-boundary
  friction). With the runner on the host, Compose would orchestrate only the task service,
  which isn't worth it, so **Compose is dropped**.

## Decision

### Three deterministic roles — all host processes

- **Task service** — control plane: task state (sole DB authority, ADR 0006), integrity
  enforcement, workflow registry. Runs as a host process (a daemon); REST + MCP; owns the
  SQLite DB + file artifacts on the local filesystem. No host coupling beyond the filesystem.
- **Session service (runner)** — realizes the execution-backend ABC (ADR 0006) as a **host
  process**. Spawns/stops task containers **directly on the host Docker daemon** (no Compose,
  no Docker-out-of-Docker socket mount — it is already on the host), owns the **host-level
  tmux** sessions (one per task; panes exec into the task's container(s)), injects per-task
  secrets (ADR 0007), and builds/runs composed images (ADR 0005). A **client of the task
  service**.
- **Terminal controller** — the user-facing `panopticon` CLI host process. Owns the TTY,
  runs the dashboard (a REST client of the task service), and on `t` attaches the terminal to
  a task's session via plain `tmux attach` and rejoins the dashboard on detach. Also a client
  of the task service.

All three are **deterministic — no LLM calls**. LLM work happens only inside task
containers, which are the **only containers** in the M1 topology.

### M1 topology: three host processes + N task containers

- The runner launches **task containers on the host daemon** and manages their tmux session.
  (Per-task Docker-in-Docker, if a workflow/repo needs it, is a separate concern *inside* a
  task container per ADR 0005 — unrelated to our services.)
- **Task containers connect back to the task service** and stay connected, registering that
  this container is working on a given task (liveness); they use MCP (+ some REST) thereafter.
  They reach the host task service via a host-accessible address injected at launch.
- **The runner registers with the task service** as an available runner.

### Remote execution (Milestone 5) is the same shape, deployed differently

Run a **runner host process per machine**, each pointed at the central task service (which
stays single and the sole DB writer — runners talk to it over REST, never the DB directly).
Adding a machine = starting a runner there and registering it; no control-plane change. The
terminal controller reaches a remote task's tmux over ssh.

## Why this topology

- **The seam is still enforced.** Task service, runner, and terminal controller are separate
  processes communicating over REST; they cannot accidentally reach across the boundary —
  the modularity the rewrite exists to achieve, without Compose machinery.
- **Runner on the host is the natural fit** for something that drives host tmux, the user's
  terminal handoff, and container spawning. It also removes the cross-boundary tmux-attach
  friction entirely.
- **Remote becomes deployment, not a rewrite.** M1 local and M5 remote are the same
  components on different hosts.
- **Independent lifecycles.** The three processes start/stop/restart independently — keeping
  the TTY decoupled from the daemons that hold container liveness.

Accepted cost vs. Compose: no declarative orchestration/restart policy out of the box, so
starting and supervising the host-process daemons is our responsibility (§ open questions).

## Consequences

**Positive**
- Simpler runtime than Compose for M1: plain processes, direct host Docker access, host
  tmux, trivial `tmux attach`.
- Clean control-plane / runner / terminal split; each independently testable and
  substitutable.
- Resolves the prior tmux-server-location and cross-boundary-attach questions (host tmux).
- Milestone 5 is a deployment topology, not an architectural change.
- Reinforces ADR 0006: the task service stays the single DB authority; runners are clients.

**Negative / open questions**
- **Process supervision.** With Compose gone, how the three daemons are started, kept alive,
  and stopped (the `panopticon` CLI launching them on demand, systemd, or a small supervisor)
  needs a lightweight answer — not blocking M1.
- **Container → host-service addressing.** How a task container reaches the host task service
  (host gateway address / injected URL) is an implementation detail for the runner.
- **Runner registration & discovery protocol** — how a runner announces itself, reports
  capacity/health, and is selected for a task (likely runner-initiated/pull, NAT-friendly for
  M5).
- **Inter-process auth & networking** — trivial on one host; real once a runner is remote
  (M5): transport security, authenticating runners, reaching the task service across networks.
- **Failure handling** — what happens to a task (and its container) if the runner or task
  service restarts; reconciliation of in-flight sessions on reconnect.
- **Artifact reach for remote runners/containers** (ADR 0003) — likely via the task service's
  MCP/REST rather than a shared filesystem.

## Related

- ADR 0006 — task service as control plane / sole DB authority; the runner is its
  execution-backend realized as a separate client.
- ADR 0005 — composed images the runner builds/runs.
- ADR 0007 — per-task secret injection performed by the runner at launch.
- ADR 0002 — dashboard (terminal controller) as a REST client; substitutable.
- ADR 0003 — artifacts via the task service's MCP surface (relevant to remote runners).
- GOALS.md — Milestone 2 (separate planner/implementer containers per task) motivates tmux
  living outside the agent container; Milestone 5 (remote execution) as per-machine runners.
- Process supervision, runner registration, and inter-process auth belong in
  `docs/design/ARCHITECTURE.md`.
