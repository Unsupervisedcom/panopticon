# 0011 — Container provisioning: a writable per-task clone, branched on slug

- Status: Proposed
- Date: 2026-06-16
- Deciders: Charlie Scherer

## Context

ADR 0010 decided *who* provisions (the session service, on the host where the container runs),
*how it's coordinated* (it observes the slug over its pull loop), and *that the task service only
records the result*. It built that out host-side: `record_provisioning`, a `Provisioner`, a
`CloneCache`, and the `ProvisionDaemon` pull loop. It left the **container-side mechanics** open:
how the agent gets something to plan against before it has a slug, how it ends up working *in* the
worktree, the mount layout, and the read-only base.

A first exploration used a shared-clone **`git worktree`** exposed through a `/workspace/repo`
**symlink** the session service swapped base→worktree on provisioning, with the clone and
worktrees **path-mirrored** into the container. It worked, but carried three accidental costs:

1. A `git worktree` keeps its object store in the shared clone and records **absolute** gitdir
   links, so it only behaves if mounted at the *same* path git recorded — hence path-mirroring,
   and hence the symlink (a `cd` through it lands the *physical* cwd on git's recorded path).
2. The base was read-only **by convention** only (the clone mount had to be writable for worktree
   commits), not enforced.
3. A live **repoint** step (swap the symlink once provisioned) plus the cwd-can't-be-moved dance.

All three exist only to make a *shared-clone worktree* usable at a friendly container path.

## Decision

Use a **writable per-task local clone**, created at spawn and **branched on slug** — not a
shared-clone worktree. A `git clone --local` is *self-contained* (its own objects — hardlinked
from the cache, so creation is near-free on one filesystem — refs, config, HEAD), so it mounts at
any container path with **no path-mirroring, no symlink, and no repoint**.

### 1. One writable clone per task, mounted read-write

At spawn-prep the session service makes the repo's cache clone current (`CloneCache`) and
`git clone --local <cache-clone> <per-task-dir>` — a full checkout on the repo's base branch. It
bind-mounts that dir **read-write** into the container at one stable path (`/workspace`). The
agent works here for the **whole** task — planning *and* coding. There is **no read-only reference
copy and no second path**: the checkout exists from the moment the container starts, so there's no
"before the worktree exists" gap to fill.

### 2. Provisioning = branch whatever's there

The agent plans against `/workspace`, decides a slug, and sets it (`PUT …/slug`). The session
service's pull loop observes the slug and provisions by **branching the current state**:
`git -C <clone> checkout -b panopticon/<slug>` (from whatever HEAD is there), repoints `origin`
at the repo's real forge URL (a `--local` clone's `origin` is the cache), and records
`(branch, clone path)` on the task service (`record_provisioning`, slug-gated). No worktree, no
repoint, no `cd` — the agent keeps working in the same `/workspace`, now on its feature branch.

### 3. The agent never moves

Because the agent works in one writable clone from the start, ADR 0010's cwd problem (the host
can't relocate a running process's cwd) **doesn't arise** — there is nothing to repoint. The agent
learns it's provisioned the same pull way it learns everything else: the recorded `branch` on
`GET /tasks/{id}`. Its planning skill waits for that before it starts committing, so commits land
on the feature branch. (If it commits beforehand, those commits are on the local base branch and
are simply carried along when `checkout -b` creates the branch from that HEAD — they are never
pushed to the base; the workflow's PLANNING gate keeps real coding after the slug regardless.)

### 4. The handshake, end to end

1. Spawn-prep: `CloneCache.ensure` (fetch); `git clone --local` → per-task clone on base; bind-mount
   it read-write at `/workspace`; spawn the container; add the task to the `ProvisionDaemon` watch set.
2. The agent plans in `/workspace`, decides a slug, sets it.
3. The daemon observes the slug → `git checkout -b panopticon/<slug>`, point `origin` at the forge,
   record `(branch, clone path)` on the task service.
4. The agent sees the recorded `branch` (poll) → codes in `/workspace` (same dir, now on its branch),
   pushes/opens its PR against the forge.

### 5. Per-task config dir for `--continue`

The per-task dir (a sibling of the clone, e.g. `<per-task-dir>/.agent`) holds the agent's
`CLAUDE_CONFIG_DIR`, persistent across container re-creation — distinct from the per-repo creds
volume (ADR 0007). A re-created container re-mounts the same dir, so `claude --continue` resumes
the task (closes ADR 0010 §5 / PR #41/#43's cross-restart gap). Credentials are still symlinked in
from the per-repo creds mount (PR #43); only the *location* moves to the per-task host dir.

### 6. Remote (M5)

Clone and branch happen where the container runs (its session-service host); nothing crosses the
network, and the task service still only records refs. Unchanged from ADR 0010.

### 7. Teardown

Stop the container, then `rm -rf` the per-task dir — the clone is self-contained, so there's no
`git worktree remove`/`prune` bookkeeping and nothing dangling in the cache. The cache clone stays.

## Why this shape

- **Simplest correct writable copy at a stable container path** — self-contained clones mount
  anywhere, so the symlink, path-mirroring, and repoint all disappear.
- **Genuinely matches task isolation** — each task owns its refs/config/HEAD; no shared-clone
  constraints (same-branch-checkout rule, prune/lock bookkeeping, `packed-refs` contention).
- **One fewer moving part in the loop** — provisioning is a branch + a record; no repoint pass.

## Consequences

**Positive**
- Deletes the read-only base, the symlink/repoint machinery, and path-mirroring.
- Teardown is `rm -rf`; no worktree admin to reconcile.

**Negative / open**
- **Per-task object store.** `git clone --local` hardlinks objects at creation (cheap), but each
  task then fetches base updates independently → some object duplication at scale. Acceptable at
  M1 (few tasks); revisit with `--reference`/alternates **only if** disk/fetch pressure becomes
  real — alternates reintroduce an absolute external dependency (the very coupling we're avoiding),
  so they'd come back paired with path-mirroring, as a deliberate scale trade.
- **`origin` rewrite.** The per-task clone's `origin` is the cache; provisioning must repoint it at
  the real forge URL so pushes/PRs target the remote.

## Supersedes / supplements

- **Supersedes** the shared-clone-worktree mechanics: ADR 0010 §3–§4 (the read-only checkout the
  agent moves into) and this ADR's own earlier "path-mirrored mounts + repo-symlink repoint" draft.
  ADR 0010's *ownership / pull-observation / control-plane-records* decisions still hold.
- `core/git.py` gains `clone --local` + `checkout -b` ops; the shared `git worktree` ops are no
  longer used for tasks. The `Provisioner` branches instead of worktree-adds; the `ProvisionDaemon`
  drops its repoint step; the workspace symlink manager is removed.

## Related

- ADR 0010 — provisioning ownership, slug-observed-via-pull, control-plane-records (still holds);
  its read-only-base + agent-moves-itself mechanics are superseded here.
- ADR 0009 — remote execution; clone+branch live where the container runs.
- ADR 0007 — per-repo creds volume, distinct from the per-task config dir (§5).
- PR #41 / #43 — container-local `CLAUDE_CONFIG_DIR` + creds symlink; §5 moves the location to the
  per-task host dir to survive container re-creation.
- ARCHITECTURE §8.3 (slug decided in-container), §9 (slug → branch → provisioning).
