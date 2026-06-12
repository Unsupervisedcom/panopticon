# 0006 — The task service (the core runtime)

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

Across the ADRs, a hexagonal "core" has been described abstractly: it owns structured
task state (ADR 0001), drives per-workflow state machines and the `agent`/`user`
**turn** (ADR 0004), and is what the dashboard (ADR 0002), in-container agents, and MCP
artifact serving (ADR 0003) all talk to. ADR 0004 named the concrete form of this core:
a **task service** that wraps the task database and loads workflow classes on startup via
path-based registration.

This ADR defines that task service as a first-class component, because every port we've
decided hangs off it and two milestones (M4 web dashboard, M5 remote execution) depend on
its shape.

## Decision

The **task service** is panopticon's core runtime: a single long-running service that is
the **sole authority over task state** and the host for orchestration logic.

### Responsibilities

- **Owns the repository port (ADR 0001).** It is the only component that reads/writes the
  task database directly. All task-state mutations go through it — clients never touch the
  DB. This makes the service the serialization point that gives ADR 0001's required
  concurrency/integrity guarantees (multiple host commands and in-container agents mutate
  state *through the service*, not as N independent DB writers).
- **Hosts the workflow registry (ADR 0004).** On startup it loads all workflow classes
  via path-based registration; it instantiates/dispatches to the active workflow for each
  task.
- **Drives task lifecycle.** Applies workflow-defined transitions, runs responsibility settlement,
  invokes workflow imperative methods (provisioning, remote VCS integration, hooks,
  cleanup), and maintains the turn-tracking mechanism (workflows only supply the fg/bg
  classification).
- **Coordinates the other ports.** Artifact store (ADR 0003), execution backend
  (containers / remote, M5 + ADR 0005 composed images), and per-repo secret injection
  (env vars + creds mounts, ADR 0007). The presentation layer (ADR 0002) is a *consumer*,
  not a coordinated port.

### Client/server seam

The task service exposes a **well-defined API**; everything else is a client of it:
- the **dashboard** (ADR 0002 presentation port) renders from and issues commands to the
  service — including idea input now that the dashboard is an input surface (PARITY §5);
- **in-container agents** reach it over the network (and reach artifacts via the MCP
  surface of ADR 0003, which the service fronts);
- **host commands** are thin clients of the same API.

The API is designed to be **network-exposable** from the start (even if initially bound
locally), because Milestone 4 (web-hosted dashboard) and Milestone 5 (tasks on external
machines) both require reaching the service across a network. This matches ADR 0002/0003's
guidance to keep the core's boundaries transport-friendly.

## Consequences

**Positive**
- **Resolves ADR 0001's concurrency concern structurally**: a single writer (the service)
  over the DB, instead of many processes racing on it.
- One coherent home for the workflow registry, lifecycle driving, and turn-tracking.
- A clean client/server boundary that Milestones 4 (web UI) and 5 (remote tasks) extend
  rather than retrofit.
- Keeps the presentation port honest: the dashboard can only do what the service API
  allows, which is what makes the dashboard genuinely substitutable (ADR 0002).

**Negative / open questions**
- **Transport & API shape** (in-process library vs. local IPC vs. HTTP/gRPC) is not yet
  fixed. Leaning network-capable for M4/M5, but the initial form can be simpler; resolve
  in ARCHITECTURE.md.
- **Single point of failure / lifecycle.** One authoritative service is a SPOF; its
  start/stop, recovery, and what happens to in-flight tasks if it restarts need design.
- **AuthN/Z.** Once network-exposed (M4/M5), who may talk to the service — and how it
  guards secrets and the privilege to run agent/workflow code — becomes a real security
  concern (ties to ADR 0004's trust stance and the per-repo secret store).
- **Reaching it from containers** (endpoint injection/forwarding into task containers)
  depends on the execution-backend design (ADR 0005 / M5).

## Related

- ADR 0001 — repository port; the task service is its sole owner and the serialization
  point for the integrity guarantees it requires.
- ADR 0002 — presentation port; the dashboard is a client of the task service API.
- ADR 0003 — artifact store + MCP; fronted by the task service.
- ADR 0004 — workflow ABC + path-based registration loaded by the task service.
- ADR 0005 — composed images the service selects/runs via the execution backend.
- GOALS.md — Milestone 4 (web dashboard) and Milestone 5 (remote execution) build on the
  service's client/server seam.
- The concrete API surface, transport, and the service's relationship to the
  execution-backend port belong in `docs/ARCHITECTURE.md`.
