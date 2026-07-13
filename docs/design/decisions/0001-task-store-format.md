# 0001 — Task store: structured database, backend-agnostic

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

panopticon needs a store for task state: the lifecycle state machine
(PLANNING → ITERATING → REVIEW → MERGING → COMPLETE), the turn (whose move it is),
git/PR/CI metadata, container/session info, and a history log (append-only but for the
current entry's in-flight responsibilities). The store is read by the dashboard and written
concurrently by host commands and in-container agents.

> **Scope:** this ADR covers the **structured** task state. Freeform per-task
> artifacts (plan files, notes) are *not* stored in the database; they are
> file-backed and served via MCP / the filesystem / the dashboard. See ADR 0003.

We evaluated plain-text options (org-mode, Markdown + frontmatter, TOML), chosen
for inspectability and git-diffability. They were rejected as the source of truth
because:

- **Query/aggregation** — the dashboard needs to filter and sort across all tasks
  (by state, mode, CI status). Plain-text means scanning and parsing every file.
- **Concurrency/integrity** — concurrent writers need transactional guarantees the
  filesystem doesn't provide cleanly.
- **org-mode specifically** carries library risk (single-maintainer parsers, fragile
  round-trip).

## Decision

The task store is a **structured database**. It **starts as SQLite**, but all code
that touches it is **agnostic to the backing database**, so the backend can later
move (e.g. to Postgres) without changing domain logic.

Concretely:

1. **Store / interface boundary.** All task-store access goes through a store
   interface. Domain and command code depend on the interface,
   never on the database driver. SQL and driver imports live only in the adapter.
2. **No backend-specific features in domain code.** Avoid SQLite-only SQL, pragmas,
   or type quirks leaking past the adapter. Anything backend-specific (e.g. SQLite
   `PRAGMA`, JSON column handling) is encapsulated in the adapter.
3. **Migrations from day one.** Schema is managed by a migration tool, not ad-hoc
   `CREATE TABLE`, so the SQLite → other-backend path and schema evolution are
   first-class.
4. **Schema decoupled from domain model.** Map between storage rows and domain
   objects at the adapter boundary, so storage shape can change independently.

The concrete ORM / query-builder / migration tooling is **deferred to the
implementation-language ADR** (still open), since it depends on language:
- Python → SQLAlchemy Core/ORM + Alembic gives backend-agnosticism + migrations directly.
- TypeScript → a backend-portable query builder (e.g. Kysely/Drizzle) behind the store interface.

## Consequences

**Positive**
- Queryable, sortable, filterable task state for the dashboard.
- Transactional integrity for concurrent writers.
- Clean upgrade path to a server database without rewriting domain code.

**Negative — and mitigations**
- **Loses plain-text inspectability**, the property we earlier valued (no `cat`,
  no hand-edit, no git-diff of state transitions). Mitigate with:
  - A first-class CLI for human inspection (`task ls`, `task show <id>`) so the
    common case doesn't require a DB tool.
  - Ad-hoc inspection via `sqlite3` / Datasette while the backend is SQLite.
  - Optionally, an **export command** to dump tasks to a git-diffable plain-text
    snapshot (Markdown/JSON) for debugging and history — read-only, not the source
    of truth.
- **Backend-agnosticism requires discipline.** It is easy to let SQLite specifics
  leak. Enforce by exercising the store against a second backend (or at least
  an in-memory variant) in tests, so leakage is caught early.

## Related

- Supersedes the plain-text task-store exploration (org-mode / Markdown+frontmatter / TOML).
- The lifecycle state machine and its legal transitions should be enforced at the
  store boundary (the "golden state-machine harness" artifact in TODO.md).
- Blocks on / informs the implementation-language ADR (tooling choice).
