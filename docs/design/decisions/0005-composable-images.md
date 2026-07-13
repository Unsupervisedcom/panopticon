`# 0005 — Composable workflow/repo container images

- Status: Accepted
- Date: 2026-06-11
- Deciders: Charlie Scherer

## Context

cloude-cade ships one monolithic Docker image (Node20/Bookworm + git, tmux, jq, gh,
`claude`, uv, bun, Docker-in-Docker). The PARITY categorization rejected that as-is and
described a layered model instead:

- Base image — "start w/ minimal image to support claude and other general panopticon
  requirements, allow workflows and repos to layer onto this" (§7).
- Docker-in-Docker — "configurable per repo" (§7).
- Entrypoint — "per configurable images" (§7).
- Repo pre-launch hooks — "built into repo configurable images" (§8).
- Setup — "add targets for workflow & repo specific images" (§13).

ADR 0004 reinforced this: a workflow contributes image layers via its provisioning
extension point. And panopticon is multi-repo with per-repo configuration (ADR 0001 repo
entity, per-repo secrets).

## Decision

A task's container image is **composed from layers**, not a single fixed image:

1. **Base image** — minimal and general. The panopticon agent-runtime essentials only:
   an agent CLI (e.g. `claude`, but see Milestone 3), git, tmux, the in-container command
   surface, and the language runtime. Deliberately small; **not** opinionated about any
   workflow or repo.
2. **Workflow layer** — adds what a workflow's imperative steps need. E.g. a forge-
   integrating workflow layers in `gh`; a workflow that runs tests layers in its test
   tooling. Contributed by the workflow (ADR 0004 provisioning extension point). A
   minimal/free-form workflow may add nothing.
3. **Repo layer** — adds repo-specific setup: build toolchain, dependencies, and the
   repo's pre-launch configuration/hooks (the cloude-cade `repo-hooks/<repo>` concept,
   now baked into the image rather than run ad-hoc).

The **effective image for a task = base + workflow layer + repo layer**, composed by
`FROM` chaining (each layer built on the one below). Default composition order is
**base → workflow → repo** (repo is the most specific and most frequently changing, so
it sits on top), but workflow and repo layers are largely orthogonal — see open
questions.

Supporting decisions:
- **Optional features are layer choices, not base defaults.** Docker-in-Docker is
  included only when a repo/workflow needs it (PARITY: "configurable per repo"), not
  baked into the base.
- **The entrypoint is base-provided but extensible.** The base ships the general
  entrypoint (cred persistence, command registration, privilege drop); layers may extend
  it for their needs.
- **Images are built via explicit, cached build targets** keyed by identity, with a
  naming scheme like `panopticon-<workflow>-<repo>` (PARITY §13: per-workflow & per-repo
  image targets). Built on demand and cached; rebuilt when their inputs change.

## Consequences

**Positive**
- Small, general base; per-workflow and per-repo customization without a bloated
  one-size image.
- Directly serves Milestone 3 (other agent CLIs — a different base or workflow layer) and
  Milestone 5 (remote execution — the composed image is the unit shipped to a remote).
- The workflow/repo split in the image mirrors the same split in ADR 0004, keeping the
  mental model consistent.

**Negative / open questions**
- **Layering order & orthogonality.** base → workflow → repo is the default, but a repo's
  toolchain rarely depends on workflow tools and vice-versa. If the two are truly
  orthogonal, linear Docker layering forces an arbitrary order and hurts cache reuse
  across the (workflow × repo) matrix. Resolve in ARCHITECTURE.md (possible answers:
  fixed order + accept some cache loss; or a build that merges two independent layer
  sets).
- **Image matrix & storage.** (workflow × repo) combinations multiply images. Where they
  live — local Docker vs. a registry — matters especially for Milestone 5 (a remote
  machine needs to pull the composed image). Deferred to the execution-backend design.
- **Rebuild triggers.** What invalidates a workflow or repo layer (workflow code change,
  repo dependency change) must be defined so stale images aren't reused.
- **Where workflow/repo layer definitions live** ties to ADR 0004 discovery (registered
  paths) and the repo entity (ADR 0001).

## Related

- ADR 0004 — workflows contribute image layers via the provisioning extension point.
- ADR 0001 — the repo entity; per-repo image configuration hangs off it.
- ADR 0006 — the task service / execution flow that selects and runs the composed image.
- GOALS.md — Milestone 3 (other CLIs) and Milestone 5 (remote execution).
- The build orchestration, image naming, and storage/registry strategy belong in
  `docs/design/ARCHITECTURE.md` and the (future) execution-backend interface.
