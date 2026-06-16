# 0010 — Task provisioning: the session service owns worktrees; the slug is observed, not pushed

- Status: Proposed
- Date: 2026-06-16
- Deciders: Charlie Scherer

## Context

A task's branch + worktree are named from its **slug** (`panopticon/<slug>`), and the slug is
**decided in the container** (ARCHITECTURE §8.3) — by the agent, after it has read the repo and
planned. So at container start there is no slug, hence no worktree; the agent needs *something*
to read while planning, and must end up working *in* the worktree once it exists.

The pieces exist but aren't wired (docs/BACKLOG.md): `core/git.py` (`GitWorktrees`),
`Workflow.provision`, and `TaskService.provision_task`. Two problems block wiring them as-is:

1. **`provision_task` runs `git worktree` on the task service's host.** Fine while everything is
   co-located, but the task service is the control plane and may be on a *different machine* than
   the container at M5 (ADR 0009). The worktree must live where the **container** runs.
2. **A running process's cwd can't be moved by the host.** Repointing a symlink (or bind-mount)
   under a process that already `cd`'d there does **not** change its cwd — cwd binds to the
   directory inode, not the path. So we can't transparently "swap" the agent's directory from a
   read-only checkout to the worktree once the slug lands.

And the open question that motivated this ADR: **how does the task service tell the session
service the slug has been set**, so it can provision?

## Decision

### 1. The session service owns host provisioning

The per-machine **session service** (ADR 0008/0009 — the host process that already spawns
containers, owns host tmux, injects secrets, builds images) creates the slug-named branch +
worktree **on the host where the container runs**, using the agnostic `core/git.py` ops, and
manages that host's per-repo **clone cache** and worktree teardown. The **task service does no
host filesystem work** — it stays the sole DB authority (ADR 0006) and remains correct when it's
remote from the container (ADR 0009).

`Workflow.provision` splits accordingly: host-touching steps run on the session service (next to
the worktree); anything that's just a recorded fact stays a deterministic task-service write.
`TaskService.provision_task` stops doing the `git worktree` itself and instead **records** the
provisioning result (branch, worktree path) reported by the session service, still slug-gated.

### 2. Coordination is **pull**, not push — the session service *observes* the slug

The task service does **not** notify the session service. The session service is already a
**client that pulls** from the task service (ADR 0008's "runner-initiated/pull, NAT-friendly";
ADR 0009's "daemon that pulls assigned work"); the slug is just one more piece of task state it
reads in that same loop. When it sees that a task it is running has acquired a slug, it
provisions. The task service only does what it already does — record the slug on `PUT
…/slug` — and answer reads.

Mechanism: **poll** `GET /tasks/{id}` (or a batch endpoint for the daemon's assigned tasks) and
act on the slug→set transition. The poll interval is the only latency knob; a long-poll/`?wait`
variant can cut latency later **without changing the direction**. Slug-set is a one-time
transition per task, so this is cheap.

Rejected — **push** (the task service opens an SSE/webhook to the session service): it makes the
control plane dial *out* to runners, which breaks behind NAT and inverts the client/server
relationship the rest of the system relies on (containers and runners always dial the task
service, never the reverse). Rejected — **the container signals the session service directly**
(bypassing the task service): it couples the container to a co-located session service, and the
task service is both the authoritative slug record and the natural coordination point.

### 3. The agent starts on a read-only checkout and **moves itself** into the worktree

The session service mounts a per-task host directory into the container at a stable path (e.g.
`/workspace`), in which a `repo` entry is **read-only** base-branch content at first (planning
material) and is repointed to the **writable worktree** once provisioned. Because the host can't
move a running process's cwd (problem 2), the **agent `cd`s into the worktree itself** as a
workflow step once it's ready — it's an LLM following instructions, not a process we silently
relocate. The agent learns "provisioned" the same pull way it learns everything else: the task
service exposes the recorded worktree path once the session service reports it, and the agent's
planning skill waits for it, then enters.

(The symlink/bind target must live *inside* the mounted tree so the container can follow it; the
exact repoint mechanics are an implementation detail. What matters is: agent enters explicitly,
no reliance on auto-follow.)

### 4. The handshake, end to end

1. Session service spawns the container and mounts the per-task dir with a **read-only** repo at
   `/workspace/repo`.
2. The agent plans against `/workspace/repo`, decides a slug, and sets it (`PUT …/slug`).
3. The session service's poll loop sees the slug → `git worktree add -b panopticon/<slug>` in its
   clone → repoints `/workspace/repo` to the worktree → reports `(branch, worktree path)` to the
   task service, which records them and runs the deterministic part of `Workflow.provision`.
4. The agent sees the task is provisioned (poll) → `cd /workspace/repo` (now the worktree) →
   continues the work there.

### 5. Per-task session persistence (closes the `--continue` gap from PR #41)

PR #41 moved `CLAUDE_CONFIG_DIR` to a *container-local* dir, so claude's session transcripts no
longer collide in the per-repo creds volume — but they don't survive container re-creation. The
session service, now the owner of per-task host state, can mount a **per-task persistent
location** for the agent's config dir (distinct from the per-repo creds volume and from the git
worktree, which shouldn't carry claude state). That lets `claude --continue` resume a task across
container restarts, not just within one container's life.

## Why this shape

- **The control plane stays pure and remote-ready** — no host FS, no dialing out; still the sole
  DB authority. Adding a machine is still just "run a session service there" (ADR 0009).
- **Worktrees live where the work runs**, which is the only correct place once the container is
  remote.
- **One coordination mechanism** (the session service's existing pull loop) carries assignment,
  slug, and lifecycle — NAT-friendly, no new inbound channel.
- **The cwd problem is sidestepped**, not fought: the agent moves itself; we never try to remount
  under a live process.

## Consequences

**Positive**
- M1 local and M5 remote use the same provisioning path (session service on the host).
- Reuses the work-pull loop; no push infrastructure.
- Unblocks the backlog "wire provisioning into the lifecycle" item.

**Negative / open questions**
- **Poll latency** between slug-set and worktree-ready (acceptable; long-poll later if needed).
- **Clone-cache management** on each session-service host: where clones live, concurrency across
  a repo's tasks, disk/GC.
- **Repoint mechanics**: symlink within the mounted tree vs. a per-task bind layout; how the
  read-only base is materialized.
- **`Workflow.provision` split**: exactly which steps are host-side (session service) vs recorded
  fact (task service), and the reporting endpoint shape.
- **Per-task persistent config location**: provisioning, mounting, and teardown of it.
- **Teardown ordering**: worktree removal vs container stop vs task completion/drop.
- This is also where the **session-service daemon** itself gets built out — today it's a one-shot
  spawn primitive (`python -m panopticon.sessionservice <task_id>`) plus the `Runner` adapter; the
  observe-and-provision loop is new, long-lived, per-host state.

## Related

- ADR 0011 — resolves this ADR's open container-side mechanics: path-mirrored mounts, the
  `/workspace/repo` symlink repoint, the read-only base, per-task config dir, and teardown order.
- ADR 0008 — session service as a host process; "runner registration/discovery … runner-initiated/
  pull, NAT-friendly" (this ADR commits to pull for slug observation too).
- ADR 0009 — remote execution; runner-per-host that pulls; the task service may be remote.
- ADR 0006 — task service as sole DB authority; everyone else is a REST client.
- ADR 0004 — the provision seam (`Workflow.provision`) and local-git-is-core.
- ADR 0007 — the per-repo creds volume (distinct from the per-task worktree and config location).
- PR #41 — container-local `CLAUDE_CONFIG_DIR`; §5 here closes its cross-restart `--continue` gap.
- ARCHITECTURE §8.3 (slug decided in-container), §9 (slug → worktree → provisioning);
  docs/BACKLOG.md "wire provisioning into the lifecycle".
