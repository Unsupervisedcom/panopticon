# 0003 — Task artifacts: file-backed, served via MCP, viewable everywhere

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

ADR 0001 puts the **structured** task state (lifecycle, mode, git/PR/CI metadata,
history) in a database. But a task also has **less-structured artifacts** — most
importantly a plan file, plus design notes and agent scratch. These are prose, they
can be large, they are authored and edited by both agents and humans, and users want
to open them directly (in a text editor, or inside the dashboard).

Forcing this content into the database would reintroduce exactly the problems that
made us reject plain-text *for structured state* in reverse: it would make the prose
hard to inspect, hand-edit, and diff. The right split is structured state in the DB,
freeform artifacts as files.

## Decision

A task is **structured record (DB) + a set of file-backed artifacts**.

1. **Artifacts are files on disk; the filesystem is their source of truth.** They live
   in a per-task directory resolvable from the task id, e.g.:

   ```
   tasks/<task-id>/
     plan.md
     notes.md
     ...
   ```

   The database (ADR 0001) stores only *references/metadata* for artifacts (relative
   path, kind, and optionally a content hash/version), never the content itself.

2. **One set of bytes, three views.** The same file is reachable through:
   - **MCP** — artifacts are exposed as MCP resources (and, where read/write is needed,
     tools), keyed by a stable URI derived from task id + artifact name. This is how
     in-container agents read and update the plan through the protocol.
   - **Filesystem** — users open `tasks/<id>/plan.md` directly in any text editor.
   - **Dashboard** — the dashboard can render/open the plan inline.

   None of these is a separate copy; they are access surfaces over the same file.

3. **Access goes through an artifact-store interface.** Consistent with the interface-and-adapters spine
   (ADR 0001 store interface, ADR 0002 presentation interface), artifact access is behind an
   **artifact-store interface**. The initial adapter is the local filesystem; the interface keeps
   the door open to other backends later (e.g. object storage) without changing core or
   MCP logic. The MCP server and the dashboard are *consumers* of this interface, not
   bypasses around it.

## Consequences

**Positive**
- **Recovers the inspectability we wanted**, scoped correctly: structured state stays
  queryable in the DB; prose artifacts stay `cat`-able, hand-editable, and git-diffable
  as plain files.
- Agents get artifacts through MCP without a panopticon-specific client; humans get the
  same content through an editor or the dashboard.
- The DB stays small and structured (pointers, not blobs).

**Negative — and things to resolve in design**
- **Two stores, one task → consistency.** A task's truth is split across DB rows and
  files. Keep the DB authoritative for *structured state* and the filesystem
  authoritative for *artifact content*; the DB's artifact metadata (path, hash) must be
  kept in sync on write. Define what happens when a file is edited out-of-band (e.g. the
  dashboard/MCP re-reads from disk; a stored hash detects drift).
- **Concurrent writers.** The plan can be edited by an in-container agent (via MCP), a
  user in an editor, and the dashboard at once. Decide a concurrency policy at the
  artifact-store interface (e.g. last-write-wins with hash-based conflict detection, or
  advisory locking) rather than per-call-site.
- **Path/URI mapping must be one function.** Task id → directory → MCP URI must be a
  single shared resolver used by the filesystem layout, the MCP server, and the
  dashboard, so all three agree on where an artifact lives.

## Related

- ADR 0001 — structured task state in a backend-agnostic DB (this ADR is its
  file-artifact counterpart; 0001's Context was scoped to point here).
- ADR 0002 — substitutable dashboard; the dashboard is one *consumer* of the artifact
  store, alongside MCP and direct filesystem access.
- The artifact-store interface, the id→path→URI resolver, and the consistency/concurrency
  policy belong in `docs/design/ARCHITECTURE.md`.
