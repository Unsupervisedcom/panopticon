# 0012 — Delegating to a Kubernetes cluster from a local panopticon

- Status: Proposed
- Date: 2026-07-08
- Deciders: Nicholas Romero

## Context

Tasks run as Docker containers on the operator's machine (ADR 0008), which is the wrong
place for long-running, resource-hungry stages — training/evaluating AutoML models, sweeps,
batch jobs — that need more compute than a workstation and shouldn't die with it.

This ADR scopes deliberately small: **panopticon stays local** (task service, session
service, console, and the interactive agent container all unchanged on the workstation);
the agent gains the ability to **delegate heavy stages to a shared Kubernetes cluster** as
Jobs. The cluster is shared by **multiple users**, each running their own local panopticon,
so the design must make one user's workloads invisible to and uncontendable with another's.

Out of scope (future ADRs): running the *task container itself* in the cluster (a
`KubernetesRunner` behind the Runner ABC), deploying the control plane in-cluster (the
"operator" from the M3 press release), and DevPod-style provider abstractions. The
delegation model here requires none of them — Jobs don't dial back to the task service, so
the local-control-plane reachability problem never arises.

## Decision outline

### 1. The delegation model: agent-launched Jobs via a scoped kubeconfig

Modeled on the existing `docker_in_docker` capability (a trust escalation, off by default):

- A repo opts in via `capabilities: {"kubernetes": {"context": "…"}}`. At spawn, the
  runner mounts a **kubeconfig** into the task container (read-only, like `/creds`) whose
  credentials are scoped as §2 describes — the agent can submit and watch Jobs in its own
  namespace and nothing else.
- The agent-facing surface is a **skill** (CLI-agnostic `Skill` spec, like `provision`):
  write a Job manifest with explicit resource requests (GPU included), `kubectl apply` it,
  poll/stream logs, and record results back as task artifacts. The cluster autoscaler turns
  requests into nodes; scale-to-zero GPU pools keep idle cost at zero.
- Jobs outlive the agent: if the task container dies, the Job keeps running and a respawned
  agent re-attaches by name. Recurring loops use the same manifest as a `CronJob`.
- Results return through the task service's artifact API for small outputs; large outputs
  (models, datasets) go to object storage the Job writes directly, with the artifact
  recording the URI. Jobs never connect to the local task service.

### 2. Identity, roles, and auth

Two explicit roles, with a bootstrap handshake between them:

**Cluster admin** — owns the cluster, installs per-user isolation. Panopticon ships a
**user-namespace template** (one manifest / kustomization) the admin applies per user:

| Object | Purpose |
|---|---|
| `Namespace` `panopticon-<user>` | the user's entire blast radius |
| `ServiceAccount` `panopticon-<user>` | the identity the user's kubeconfig wraps |
| `Role` + `RoleBinding` | namespaced permissions only (see below) |
| `ResourceQuota` | the user's compute ceiling (§3) |
| `LimitRange` | per-pod defaults + maxima (§3) |
| `NetworkPolicy` | default-deny + explicit egress (§4) |

**User** — receives a kubeconfig for that ServiceAccount and points their repo's
`kubernetes` capability at it. Tokens are **short-lived**, minted via the TokenRequest API
(`kubectl create token panopticon-<user> --duration 24h`); the admin (or a small
`panopticon cluster grant <user>` helper wrapping the template + token mint) re-issues on
expiry. No long-lived SA token Secrets.

The user `Role` grants, **in the user's namespace only**:

- `create/get/list/watch/delete` on `jobs`, `cronjobs`, `pods` (delete = the agent can
  clean up its own work)
- `get` on `pods/log`, `create` on `pods/portforward` (watching training progress)
- `create/get/delete` on `configmaps` and `secrets` (job inputs the agent stages)

Explicitly absent: anything cluster-scoped (`nodes`, `namespaces`, `persistentvolumes`),
`pods/exec` (Jobs are batch, not shells — reduces lateral movement if a token leaks), and
any verb in another namespace. RBAC makes other users' namespaces not merely inaccessible
but **invisible** — `kubectl get jobs --all-namespaces` fails; there is no cross-user
`list`.

Optionally, panopticon can narrow further per task: the agent's kubeconfig wraps a
**per-task token** with a label-scoped view, so even within one user's namespace two
concurrent tasks don't touch each other's Jobs. Namespace-per-user is the isolation
boundary that matters; per-task scoping is defense in depth.

### 3. Resource limits: quota per user, bounds per pod

Two layers, both in the template so no user exists without them:

- **`ResourceQuota`** caps the namespace aggregate — the user's total ceiling regardless of
  how many tasks they run: `requests.cpu`, `requests.memory`, `limits.cpu`,
  `limits.memory`, `requests.nvidia.com/gpu`, `count/jobs.batch`, `pods`. A user who
  saturates their quota queues *their own* work; nobody else notices.
- **`LimitRange`** bounds each pod: default requests/limits (so an agent that omits them
  doesn't get scheduled unbounded) and `max` per container (so one Job can't consume the
  entire namespace quota and starve the user's other tasks).

Contention between users is then the scheduler's job, not a convention: quotas are
admission-enforced (a Job exceeding quota is rejected at `apply`, which the agent sees and
reports), and the autoscaler grows the cluster within the node-pool bounds the admin set.
Optionally a per-user `PriorityClass` lets the admin tier users; not required for
isolation.

Every object the agent creates carries labels — `panopticon.io/user`,
`panopticon.io/task-id`, `panopticon.io/slug` — and Job names are prefixed with the task
slug. Combined with namespace-per-user, two users (or two tasks) can submit `train-model`
simultaneously with no name collision, and cleanup/cost attribution are label selectors.
`ttlSecondsAfterFinished` on every Job keeps namespaces from accumulating dead pods.

### 4. Network restrictions

Each user namespace gets a default-deny posture with explicit holes:

- **Ingress: deny all.** Nothing in the cluster (including other users' namespaces) may
  connect to a user's pods. Training jobs are workloads, not services; if a job needs a
  UI (e.g. a dashboard), the user reaches it via `port-forward`, which rides the API server
  and their RBAC rather than pod networking.
- **Egress: deny by default, then allow** (a) DNS, (b) the forge + package registries +
  object storage (the artifact return path), and — per cluster policy — (c) general
  internet for dependency fetches. The admin chooses (b)-only for locked-down clusters or
  (b)+(c) for convenience; the template ships both variants.
- **No path to the control plane's neighbors**: deny egress to the cluster's pod/service
  CIDRs (except DNS) and to the cloud metadata endpoint (`169.254.169.254`) — the standard
  SSRF/credential-theft target. API-server access is governed by RBAC, not the netpol.

Since the delegation model has no Job→task-service callback, the policies never need a
hole punched back to anyone's workstation — the main simplification bought by keeping this
ADR's scope to delegation.

### 5. What panopticon ships

1. The **user-namespace template** (Namespace, ServiceAccount, Role, RoleBinding,
   ResourceQuota, LimitRange, NetworkPolicies) as versioned manifests in-repo, with the
   quota/egress values as the admin-tunable surface.
2. The **`kubernetes` capability** in the repo config + runner: mount the named kubeconfig
   read-only into the task container at spawn.
3. The **delegation skill**: manifest conventions (labels, slug-prefixed names, TTL,
   resource requests mandatory), submit/watch/log, artifact recording.
4. Optionally `panopticon cluster grant <user>` — admin helper: apply the template for a
   user, mint the token, emit the kubeconfig.

## Consequences

**Positive**
- The goal — elastic, long-running compute for training loops — with zero topology change:
  local panopticon, unchanged runner, no callback path, no in-cluster panopticon install.
- Isolation is structural, not behavioral: namespace + RBAC + quota + netpol are enforced
  by the API server at admission, so a confused (or prompt-injected) agent *cannot* touch
  another user's work — it can only exhaust its own quota.
- The template doubles as the trust document: everything a panopticon user can do to the
  cluster is readable in one manifest.

**Negative / open questions**
- **Token lifecycle**: short-lived tokens need re-minting; whether the runner refreshes
  them automatically (needs an admin-ish credential locally — undesirable) or the user
  re-runs a grant step (friction) is unresolved.
- **Artifact return for large outputs** presumes object storage the Jobs can reach and
  credentials for it — a second per-user secret the template doesn't yet cover.
- **GPU quota shape**: `requests.nvidia.com/gpu` quota is coarse (no distinction between
  one big and many small); fine if the cluster has one GPU type, revisit otherwise.
- **Cost visibility**: labels enable attribution but nothing surfaces spend per user yet.
- **Job image provenance**: the agent picks the Job image; whether to restrict to an
  allow-listed registry (admission policy) is an admin decision the template should offer.

## Related

- ADR 0008/0009 — the local topology and pull posture this leaves untouched; Jobs don't
  participate in the runner/claim model at all — they are sub-work of a locally-run task.
- ADR 0005 — `capabilities` (docker_in_docker) is the pattern the `kubernetes` capability
  copies: per-repo opt-in, trust escalation, off by default.
- ADR 0007 — the kubeconfig is a per-repo secret reference, injected at launch like
  `env_file`/`creds_volume`.
- ADR 0003 — artifacts as the results path for delegated work.
- Future ADRs — running task containers in-cluster (`KubernetesRunner`) and an in-cluster
  control plane ("the operator", per the M3 press release on the
  `milestones/M3-remote-execution-environments` branch) were surveyed in an earlier draft
  of this document (see this ADR's git history) and deliberately split out.
