# 0002 — Implementation language: Python, with a substitutable dashboard

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

panopticon rewrites the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype, which is ~68% Python / ~29% Shell. The remaining open decision after the
task-store choice (ADR 0001) was the implementation language.

The TUI dashboard (the prototype's `cloude-dash`) is a primary concern: it shows
many concurrent tasks updating in real time, so terminal-UI ecosystem quality was a
significant factor. We compared Python (Textual), Go (Bubble Tea), Rust (Ratatui),
and TypeScript (Ink). All but TypeScript are strong for a live dashboard; Go and Rust
edge out Python on raw TUI performance and single-binary distribution, while Python
offers the most continuity with the prototype and a very capable framework in Textual.

## Decision

**Use Python to start.**

Rationale:
- **Continuity** with the prototype's domain logic and the team's existing knowledge —
  fastest path to a working rewrite.
- **Textual** is a capable, modern TUI framework; performance is more than adequate for
  a task dashboard (dozens of tasks, not high-frequency telemetry).
- The cost of choosing Python now is bounded by the modularity constraint below.

**The dashboard is a substitutable module.** The design separates a UI-agnostic core
from the presentation layer, behind an explicit interface (a "presentation interface"),
exactly as ADR 0001 puts the database behind a store interface. The dashboard consumes
the core through that interface and never the reverse.

Consequences of that boundary:
- The TUI (Textual) can be replaced later — a different Python TUI, a web UI, or a
  dashboard reimplemented in another language (e.g. Go/Bubble Tea or Rust/Ratatui)
  talking to the core over a defined contract — **without rewriting domain logic**.
- "Python to start" is therefore a low-lock-in decision: the language can stay for the
  core while the most language-sensitive component (the dashboard) remains replaceable.

To keep this real (not aspirational):
- The core exposes task state and lifecycle operations through an interface with **no
  Textual/curses types leaking across it**. The dashboard depends on the core; the core
  has zero dependency on any UI library.
- Favor a transport-friendly contract at that boundary (plain data structures / a small
  command+query surface) so an out-of-process or non-Python dashboard remains feasible.

## Consequences

**Positive**
- Fastest path to a working rewrite; reuses prototype domain knowledge.
- Textual gives a strong dashboard now.
- The UI is swappable, so the language choice is revisitable where it matters most.

**Negative — and mitigations**
- Python loses Go/Rust's single-binary distribution and lower TUI resource use. Adequate
  for current scale; if distribution/perf becomes a problem, the presentation interface lets a
  Go/Rust dashboard be swapped in without touching the core.
- Modularity requires discipline: it is easy to let Textual concepts leak into core logic.
  Enforce by keeping the core importable and testable with no UI dependency installed.

## Related

- ADR 0001 (task store) — same interface-and-adapters pattern: core depends on **interfaces**
  (store for storage, presentation for UI); concrete adapters (SQLite, Textual) are
  replaceable.
- The core/presentation split and the interface contracts belong in `docs/ARCHITECTURE.md`.
