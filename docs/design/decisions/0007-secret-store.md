# 0007 — Per-repo secrets: runtime env vars + credential mounts

- Status: Accepted (amended by [0012](0012-retire-oauth-creds-volume.md))
- Date: 2026-06-11
- Deciders: Charlie Scherer

> **Amended by ADR 0012.** The per-repo OAuth **credential volume / mount** described here is
> **retired**. Container `claude` auth is now a non-rotating `claude setup-token` the operator places
> in the repo's **`env_file`** as `CLAUDE_CODE_OAUTH_TOKEN` (the runner already injects `env_file`);
> there is no `panopticon login`. The per-repo **env-file** mechanism below is unchanged — it simply
> also carries that token. See [`docs/auth.md`](../auth.md).
>
> **Amendment (remote runners).** `env_file` is stored as a **name relative to the secrets dir**
> (`$PANOPTICON_CONFIG/secrets`, default `~/.config/panopticon/secrets/`), not an absolute host
> path. Each runner resolves it against its **own** local secrets dir at launch, so the stored value
> is host-agnostic and a remote runner uses its own host's secrets — directly answering the *Remote
> delivery* open question below. The file's content still never crosses the wire (only the name is
> stored/served); the operator provisions the same-named env-file on each host that runs the repo's
> tasks. This mirrors how `image_layer_file` is stored relative to the layers dir (ADR 0005).

## Context

Milestone 1 (GOALS.md) requires **separate API keys / secrets per repo**: each repo has
its own secrets, and a task inherits the secrets of the repo it operates on. PARITY §9
generalized cloude-cade's per-repo Claude-OAuth volume to "other secrets" and moved
`GH_TOKEN` out of ad-hoc env vars.

An earlier draft of this ADR introduced a full **secret-store interface** (backend-agnostic,
references-vs-values, OS-keyring / Vault adapters). That was judged **over-engineered for
what panopticon needs**. This ADR replaces it with a deliberately minimal model.

Two authentication shapes must both be supported:
- **API-key-style secrets** (e.g. `ANTHROPIC_API_KEY`, `GH_TOKEN`, model API keys) — a
  simple key/value.
- **Interactive OAuth login** (cloude-cade's `make login`) — produces *credential token
  files*, not a single value, and must persist across task restarts.

## Decision

Per-repo secrets are provided by **two complementary mechanisms, both scoped to a repo
and both injected at container launch — never baked into an image**:

1. **Per-repo environment variables.** A repo has an env config (an env-file / config on
   the host) holding API-key-style secrets. At task launch the execution backend injects
   them into the container (e.g. `--env-file`), scoped to that repo.

2. **Per-repo credential mount.** For interactive OAuth, a per-repo persisted store of
   credential *files* (the cloude-cade `cloude-claude-creds-<repo>` volume, generalized)
   is **mounted** into the task container at launch. An interactive `login`-style command
   populates it; it persists across task restarts.

Both are **per-repo and runtime-injected**, which preserves the two properties that
actually matter:

- **Not in image layers.** Injection happens at `docker run`, not `docker build`, so
  secrets never enter shareable image layers or a registry — essential because ADR 0005 /
  Milestone 5 push composed images to remotes.
- **Out of the task database.** Secret values live in host env-files / credential volumes,
  not the DB. The repo entity (a first-class DB entity, ADR 0001) records only *that* a
  repo has an env config and/or a creds volume — not their contents — so `cat`-able task
  state and file artifacts stay secret-free.
- **Per-repo isolation.** A task receives only its own repo's env vars and creds mount,
  never another repo's.

### What was dropped vs. the earlier draft

- No backend-agnostic "secret-store interface"; no keyring / Vault adapters.
- No references-vs-values indirection beyond the repo entity simply pointing at its
  env-file / creds volume.
- The `login` flow still generalizes to other agent CLIs / credential kinds (Milestone 3),
  but is just "populate this repo's creds volume / env config," not a store API.

## Consequences

**Positive**
- Satisfies Milestone 1 (per-repo secrets) with minimal machinery — an env-file and a
  mounted volume per repo.
- Covers **both** auth shapes: env vars for API keys, mounted files for OAuth.
- Keeps secrets out of image layers (works with ADR 0005 image publishing) and out of the
  DB.
- Strong per-repo isolation by construction.

**Negative / open questions**
- **At-rest protection.** Env-files and creds volumes sit on the host with host/Docker
  permissions, unencrypted (as cloude-cade did). Acceptable for single-user Milestone 1;
  the threat model can be revisited later. Note this interacts with ADR 0004: **workflow
  code runs at orchestrator privilege and can read the secrets injected into its tasks**,
  so the "review workflow files when stakes warrant" stance extends to secret access.
- **Remote delivery (Milestone 5).** Getting a repo's env-file and creds volume securely
  to a task on an external machine is unsolved here and belongs with the execution-backend
  design — and is the main reason secrets must stay out of the (publishable) image.
- **Granularity.** Per-repo is the decided scope; finer scoping (per-workflow / per-agent-
  role) is deferred but not precluded — it would be additional env layered at injection.
- **Injection details** (exact env-file format, mount path, volume naming) belong in
  ARCHITECTURE.md / the execution-backend interface.

## Related

- GOALS.md — Milestone 1 (per-repo secrets), Milestone 3 (other CLIs), Milestone 5
  (remote / secure delivery).
- ADR 0001 — repo entity records the association to an env config / creds volume, not the
  values.
- ADR 0005 — composed images: secrets are injected at run time precisely so they never
  enter the (publishable) image.
- ADR 0006 — the task service / execution flow performs per-task injection at launch.
- ADR 0004 — trust boundary: workflow code can read injected secrets; review accordingly.
