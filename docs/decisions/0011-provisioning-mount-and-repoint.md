# 0011 — Container provisioning: path-mirrored mounts and the repo-symlink repoint

- Status: Proposed
- Date: 2026-06-16
- Deciders: Charlie Scherer

## Context

ADR 0010 decided *who* provisions (the session service, on the host where the container runs),
*how it's coordinated* (the session service observes the slug over its pull loop), and *that the
agent moves itself* into the worktree (the host can't relocate a running process's cwd). It built
that out host-side: `record_provisioning` (task service records the refs), `Provisioner`
(clone → worktree → record), `CloneCache`, and the `ProvisionDaemon` pull loop.

It deliberately left the **container-side mechanics** open (its "negative / open questions"):

- **Repoint mechanics** — symlink within the mounted tree vs. a per-task bind layout; how the
  read-only base is materialized.
- **Per-task persistent config location** — provisioning/mounting/teardown of the agent's config
  dir so `claude --continue` survives container re-creation (ADR 0010 §5).
- **Teardown ordering** — worktree removal vs. container stop vs. task completion/drop.

This ADR resolves those so the handshake (ADR 0010 §4) can be implemented. The hard constraint is
**git worktree mechanics**: `git worktree add` writes the worktree's `.git` as a *file* pointing
at `<clone>/.git/worktrees/<name>` (by absolute path), and the clone's admin entry points back at
the worktree (by absolute path). The object/ref store stays in the **main clone**. Therefore:

1. The worktree only works if the clone is present **at the same absolute path** git recorded.
2. Commits in the worktree write objects into the **clone's** object store, so the clone must be
   **writable** wherever the agent runs.
3. Both the clone and the worktree must be reachable **inside the container** at those same
   absolute paths.

## Decision

### 1. Path-mirrored bind mounts

The session service bind-mounts its host provisioning roots into the container **at identical
absolute paths**. With host and container paths equal, every absolute gitdir link git recorded on
the host resolves unchanged inside the container — no path rewriting, no per-task re-clone. Layout
under a single host root `P` (e.g. `~/.panopticon`):

```
P/clones/<repo_id>                         # shared per-repo clone (CloneCache); object store lives here
P/worktrees/<repo_id>/panopticon/<slug>    # per-task worktree (GitWorktrees)
P/tasks/<task_id>/                          # per-task dir: the repo symlink + agent config dir
```

Mounts (M1 local; see §6 for remote):

| Host path | Container path | Mode | Why |
|---|---|---|---|
| `P/clones` | `P/clones` | read-write | worktree commits write objects into the clone's store |
| `P/worktrees` | `P/worktrees` | read-write | the task's writable working tree |
| `P/tasks/<task_id>` | `/workspace` | read-write | stable cwd anchor holding the `repo` symlink |

The clone is mounted read-write because git worktree commits land in the shared object store
(constraint 2) — "read-only base" (§3) is enforced by *what the symlink points at*, not by the
clone's mount mode.

### 2. The `repo` symlink is the repoint point

The agent always works at one stable path: **`/workspace/repo`**, a symlink the session service
owns. The symlink (not a bind-mount) is the repoint mechanism — swapping a symlink is atomic,
needs no remount, and its absolute target resolves in the container because `P/...` is
path-mirrored (constraint 3; ADR 0010 §4's "target must live inside the mounted tree").

- **Before provisioning:** `/workspace/repo → P/clones/<repo_id>/<readonly-base>` (see §3).
- **On provisioning** (the `ProvisionDaemon`, right after `Provisioner` records the worktree):
  re-point `/workspace/repo → P/worktrees/<repo_id>/panopticon/<slug>`.

Because a running process's cwd binds to the inode, not the path (ADR 0010 §2), swapping the
symlink does **not** move an agent that already `cd`'d in. The agent enters explicitly (§4): it is
told `/workspace/repo` and `cd`s there *after* it observes the task is provisioned — so it only
ever resolves the symlink once it points at the worktree.

### 3. The read-only base is a detached base-branch worktree

The agent needs base-branch content to plan against before it has a slug. Materialize it as a
**second, detached worktree of the base branch** — `git -C P/clones/<repo_id> worktree add
--detach P/worktrees/<repo_id>/_base <base>` — and point the pre-provision symlink at it. It's a
real checkout (full tree, correct base), shares the clone's object store, and is **never committed
into** (detached, and the agent has no task branch yet). One `_base` worktree per repo is reused
across that repo's tasks (it tracks the base branch; refreshed when the clone fetches).

Rejected for the base: pointing the symlink straight at the **clone's own working tree** — a
clone is on *some* branch, and a second worktree can't check out a branch the clone has checked
out, so the layout gets brittle as tasks come and go; a dedicated detached `_base` sidesteps it.
"Read-only" is by construction (the agent plans here and only writes once `cd`'d into its task
worktree), not by mount mode — keeping it consistent with the read-write clone mount (§1).

### 4. The handshake, end to end

1. Session service prepares `P/tasks/<task_id>` with `repo → P/clones/<repo_id>`'s `_base`
   worktree, spawns the container with the §1 mounts, and adds the task to its `ProvisionDaemon`
   watch set.
2. The agent plans against `/workspace/repo` (read-only base), decides a slug, sets it
   (`PUT …/slug`).
3. The daemon observes the slug → `Provisioner` ensures the clone, `git worktree add -b
   panopticon/<slug>` off base, records `(branch, worktree)` on the task service → then repoints
   `/workspace/repo` to the new worktree.
4. The agent polls its task; once `worktree` is set, it `cd /workspace/repo` (now the worktree)
   and continues the work there. The planning skill carries this "wait, then enter" step.

The agent learns "provisioned" the same pull way it learns everything else — the recorded
`worktree` on `GET /tasks/{id}` — so no new channel.

### 5. Per-task config dir for `--continue`

`/workspace` (host `P/tasks/<task_id>`) is **per-task and persistent across container
re-creation**, so it is also where the agent's `CLAUDE_CONFIG_DIR` lives (e.g.
`/workspace/.agent`), distinct from the per-repo creds volume (ADR 0007) and from the git worktree
(which must not carry agent state). This closes ADR 0010 §5 / PR #41's cross-restart gap: a
re-created container re-mounts the same `P/tasks/<task_id>`, so `claude --continue` resumes the
task. Credentials are still symlinked in from the per-repo creds mount (PR #43); only the
*location* moves from container-local to the per-task host dir.

### 6. Remote (M5)

Path-mirroring is **per host**, and ADR 0010 already puts the worktree where the container runs —
i.e. on that host's session service. So a remote host mounts *its own* `P/...` into *its own*
containers at identical paths; nothing crosses the network. The task service (possibly remote)
still only records refs. No change to this scheme for M5.

### 7. Teardown ordering

On task completion/drop (the session service tearing the task down):

1. **Stop the container** first (releases open file handles on the worktree).
2. **Remove the task worktree** — `GitWorktrees.remove(..., force=…)` (PARITY §8 tiers).
3. **Remove `P/tasks/<task_id>`** (symlink + per-task config dir).

The **clone** and the shared **`_base`** worktree stay (the cache). Clone/`_base` GC, and
concurrency across a repo's tasks, remain backlog (ADR 0010).

## Why this shape

- **No path rewriting, no per-task re-clone** — mirroring makes git's absolute links Just Work
  inside the container, which is the whole difficulty.
- **Repoint is an atomic symlink swap** — no remount under a live process; pairs exactly with the
  agent entering itself (ADR 0010 §2/§3).
- **One stable cwd anchor** (`/workspace/repo`) the agent is told once; the host moves the target
  underneath *before* the agent enters.
- **Same scheme local and remote** — mirroring is per host; ADR 0010 keeps the worktree co-located
  with the container.

## Consequences

**Positive**
- Implements ADR 0010 §3/§4 with only bind mounts + a symlink; reuses `CloneCache`/`GitWorktrees`/
  `ProvisionDaemon` unchanged (the daemon gains a repoint step after recording).
- Per-task persistent `/workspace` also solves `--continue` across restarts (ADR 0010 §5).

**Negative / open**
- **Path-mirroring couples host and container filesystem layout** — the container image must not
  occupy `P/...`; we standardize `P` (e.g. `~/.panopticon` → same in-container) and keep it
  config-overridable.
- **Shared clone is read-write in the container** — an agent *could* write into `P/clones`; we
  rely on it working in its worktree (the only branch it owns) and on the `_base` being detached.
  Hardening (e.g. an overlay, or per-task object-store separation) is deferred.
- **`_base` worktree freshness** — it tracks the base branch and is only as current as the last
  `CloneCache` fetch; acceptable for planning material.
- **Disk** — clone + `_base` + one worktree per active task per repo; GC deferred (ADR 0010).

## Related

- ADR 0010 — task provisioning: session-service-owned worktrees, slug observed via pull, agent
  enters itself; this ADR resolves its open container-side mechanics.
- ADR 0009 — remote execution; the worktree lives where the container runs (per-host mirroring).
- ADR 0007 — per-repo creds volume, distinct from the per-task config dir (§5).
- PR #41 / #43 — container-local `CLAUDE_CONFIG_DIR` + creds symlink; §5 moves the location to the
  per-task host dir to survive container re-creation.
- ARCHITECTURE §8.3 (slug decided in-container), §9 (slug → worktree → provisioning).
