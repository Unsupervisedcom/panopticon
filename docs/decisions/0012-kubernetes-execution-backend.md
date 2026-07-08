# 0012 — Kubernetes execution backend: elastic environments for long-running tasks

- Status: Proposed
- Date: 2026-07-08
- Deciders: Nicholas Romero

## Context

Today every task runs as a Docker container on a runner's host (ADR 0008), and remote
execution (ADR 0009) scales by adding *hosts*, each with a fixed shape. That is the wrong
unit for a class of work we want tasks to take on:

- **Long-running loops** — training/evaluating AutoML models, sweeps, batch data jobs —
  that run for hours or days and shouldn't occupy an interactive host.
- **Bursty resource needs** — a task that plans on 2 CPUs but needs 64 CPUs / a GPU for one
  stage, then nothing.
- **Scheduled / unattended work** — the M3 press release promises exactly this: "a local
  agent delegates resource-intensive work to managed compute; self-hosters install the
  open-source Panopticon Kubernetes Operator."

Kubernetes gives us the missing primitives: declarative pod specs with resource
requests/limits, node pools + cluster autoscaling (including scale-from-zero GPU pools),
Jobs with restart/backoff semantics, and a uniform API reachable through a kubeconfig.

This ADR outlines the ways panopticon could create and deploy agents into a cluster, and
which we should build first. It does not change the control plane: the task service remains
the sole DB authority (ADR 0006) and makes no LLM calls; everything below is session-service
territory.

### Constraints inherited from prior ADRs

Any Kubernetes backend must preserve:

1. **Runners pull; the control plane never reaches out** (ADR 0009 §1). The task service
   must not hold cluster credentials or dial the Kubernetes API.
2. **Agent persistence lives where the container runs** (ADR 0009 §3). The agent must
   survive the operator's connection dropping.
3. **Detach→attach switching** (ADR 0009 §6). The console reaches a session by prefixing a
   command (`ssh -t … tmux attach` today); a cluster session must fit the same loop.
4. **Per-repo secrets are injected at launch, scoped to the task** (ADR 0007).
5. **Composed images** base→workflow→repo (ADR 0005) must reach the cluster — a registry,
   already the preferred M5 answer (ADR 0009 §4).
6. **Provisioning**: a writable per-task clone mounted at `/workspace`, branched on slug
   (ADR 0010/0011), with the host-side git happening where the container runs.
7. **New backends implement an interface; they don't change callers** — the `Runner` ABC in
   `sessionservice` is the seam.

## Options

Options A–C answer *where the task's agent container runs*. Option D answers *how a running
agent gets elastic compute for its heavy stages*. They are orthogonal; D composes with any
of A–C (and with today's Docker backend).

### A. `KubernetesRunner` — the Runner ABC realized with `kubectl`

The smallest step: a second `Runner` implementation beside `LocalRunner`, shelling out to
`kubectl` (long options, injectable command-runner — the same conventions as the
docker/tmux runner) against a kubeconfig the *session service host* holds. The runner still
runs as an ordinary host process wherever the operator starts it; only the containers move
into the cluster.

- Spawn = `kubectl apply` of a per-task **pod** (or a StatefulSet of one, for a stable
  identity across node evictions): the composed image, resource requests/limits from a new
  per-task/per-repo resource spec, the task-service URL injected as an env var. The
  container's entrypoint loop (register → slug → heartbeat) is unchanged — it dials out, so
  cluster networking only needs egress to the task service.
- tmux moves **inside the pod**: the pod runs `tmux new-session … python -m
  panopticon.container.agent` as pid 1 (or under a tiny supervisor), satisfying constraint 2
  without a host tmux. Attach becomes `kubectl exec --stdin --tty <pod> -- tmux attach` —
  a one-command prefix, exactly the shape the ADR 0009 supervisor loop expects.
- Provisioning: an `emptyDir` or per-task PVC at `/workspace`, populated by an **init
  container** that clones the repo (from the forge directly, or from a clone-cache PVC);
  the runner performs the slug-branching by `kubectl exec`-ing git in the pod, then records
  branch + clone path back exactly as today. "Host git happens where the container runs"
  becomes "git happens in the pod" — same principle, one hop further in.
- Secrets: `env_file` → a Kubernetes `Secret` mounted as env; `creds_volume` → a `Secret`
  or small PVC mounted at `/creds`. `panopticon login <repo>` gains a `--context` to
  populate the cluster-side secret (the ADR 0009 §5 "secrets are per host" rule, where the
  host is now a cluster).

**Pros:** smallest delta; reuses every existing seam (Runner ABC, entrypoint, provisioning
record); testable exactly like `LocalRunner` (pin the emitted `kubectl` commands with a fake
command-runner). **Cons:** the runner process is still a pet on some host; no reconciliation
if a pod is evicted while the runner is down; imperative `kubectl` rather than declarative
ownership.

### B. Panopticon Kubernetes Operator — a runner *in* the cluster

The M3 press-release shape: the session service itself is deployed **into** the cluster as
a Deployment (the "operator"), pulling work from the task service over REST like any runner
(constraint 1 holds — credentials for the cluster never leave it; the operator uses its
in-cluster ServiceAccount, no kubeconfig shipping at all).

Two flavours, in order of ambition:

- **B1 — in-cluster runner:** literally `python -m panopticon.sessionservice.host` in a pod,
  with `KubernetesRunner` (option A) as its backend using the in-cluster API. Installing the
  "operator" = `kubectl apply` one manifest (or a Helm chart). Everything in A applies; the
  runner is now supervised by Kubernetes itself (restart policy, no pet host).
- **B2 — CRD-based operator:** a `PanopticonTask` custom resource per task; the operator
  reconciles desired state (task claimed, not terminal) against actual (pod exists, healthy),
  giving crash/eviction recovery, `kubectl get panopticontasks` observability, and a place to
  hang policies (TTL, priority classes, node selectors). The task service remains the source
  of truth; the CRD is a *projection* the operator maintains, never a second writer.

**Pros:** self-hosted install is one manifest; Kubernetes supervises the runner (closing the
ADR 0008 "process supervision" open question for this backend); B2 buys real reconciliation.
**Cons:** B2 is a substantial new artifact (controller loop, CRD versioning) — and a second
place task state is mirrored, which we must keep visibly subordinate to the task service.
B1 first; B2 only when eviction-recovery pain is real.

### C. DevPod provider as the spawn mechanism

Instead of writing per-backend runners, drive [DevPod](https://devpod.sh/docs/managing-providers/add-provider):
the runner shells out to `devpod up` and DevPod's **provider** abstraction (`devpod provider
add kubernetes`) does the provisioning. One integration would buy every DevPod provider —
Kubernetes, SSH hosts, AWS/GCP/Azure/DigitalOcean VMs — plus devcontainer.json support,
which overlaps with ADR 0005's repo image layer.

- The runner becomes a thin `DevpodRunner`: `devpod provider add kubernetes && devpod
  provider set-options kubernetes --option KUBERNETES_CONTEXT=…`, then per task `devpod up
  <task-clone> --provider kubernetes --id task-<id>` and `devpod ssh task-<id>` for attach
  (DevPod injects its agent + ssh access into the workspace — a ready-made answer to
  constraint 3's command prefix).
- The task container's entrypoint/registration loop would run *inside* the DevPod workspace,
  launched via its post-create hooks.

**Pros:** many backends for one integration; the "give an agent a dev environment on
arbitrary infra" problem is DevPod's whole job; workspace lifecycle (stop/resume, auto-sleep)
comes free. **Cons:** a large third-party dependency in the critical path; ADR 0005's
composed-image pipeline and DevPod's devcontainer build would fight over image ownership;
secrets and provisioning flows need reshaping around DevPod's model rather than ours; less
control over pod specs (GPU node selectors, priority classes) than emitting them ourselves.
Worth a spike as a *provider of hosts*, but not the primary backend.

### D. Agent-requested burst compute: a `kubernetes` capability

Independent of where the agent itself runs: give a task a **scoped kubeconfig** so the agent
can launch Kubernetes **Jobs** for its heavy, long-running stages (the AutoML training loop)
while the interactive agent container stays small.

- Modeled exactly like `docker_in_docker` (ADR 0005 / the repo glossary): a repo opts in via
  `capabilities: {"kubernetes": {...}}`; the runner then mounts a kubeconfig at spawn whose
  credentials are a **per-task (or per-repo) ServiceAccount bound to a single namespace**
  with quota — the agent can `kubectl apply` Jobs, watch them, and pull results, and nothing
  else. A trust escalation, off by default, like DinD.
- The agent-facing surface is a **skill** (`Skill` spec, rendered per-CLI): "launch a
  training job" = write a Job manifest with resource requests (GPU included), submit, poll,
  stream logs back into artifacts. The cluster autoscaler turns resource requests into
  nodes; scale-to-zero GPU pools make idle cost zero.
- Long-running loops survive the agent: a Job keeps running if the task container dies; the
  respawned agent re-attaches by name. For scheduled/unattended loops, the same manifest as
  a `CronJob`.

**Pros:** delivers the actual goal — dynamically scalable resources for training loops —
without moving the interactive session at all; smallest security surface (namespace + quota
+ RBAC); works today with `LocalRunner`. **Cons:** results/artifacts need a path back
(object storage or the task service's artifact API); credential issuance/rotation for the
per-task ServiceAccount is new machinery.

## Recommendation

**D first, then A, then B1; keep C as a spike; defer B2.**

D is the shortest path to "an agent that trains AutoML models on elastic compute" and
touches no topology. A is the natural second `Runner` and makes the *whole task* elastic.
B1 is A deployed in-cluster and closes the self-hosting story from the press release. Each
step reuses the previous one's mechanics (secrets-as-Secrets, registry images, exec-attach).

## Consequences

**Positive**
- The Runner ABC proves out: a second real backend with no caller changes (constraint 7).
- Resource requirements become part of a task/repo's declaration — useful metadata even on
  the Docker backend.
- The registry requirement (ADR 0009 §4) gets a forcing function.

**Negative / open questions**
- **Attach ergonomics**: `kubectl exec … tmux attach` needs the operator's kubeconfig on the
  console host; the supervisor's attach command becomes per-backend data the task service
  must surface (today it's derived from host + session name).
- **Artifact reach**: in-cluster tasks can't share the task service's filesystem artifact
  store — artifacts must flow over REST/MCP (already the ADR 0008 answer for remote) or an
  object store; large training outputs (models, datasets) likely want the latter.
- **Liveness semantics**: pod evictions/reschedules look like container death today; the
  claim/respawn flow (ADR 0008) needs to distinguish "gone" from "moved".
- **Cost/quota governance**: who bounds what an agent may request (max GPUs, wall-clock TTL,
  namespace quotas) — per-repo config, like capabilities.
- **Per-cluster secrets**: `panopticon login` targeting a cluster secret instead of a Docker
  volume needs design (sealed-secrets / external-secrets integration vs. plain `kubectl
  create secret`).

## Related

- ADR 0008 — the topology (runner as execution backend; open questions on supervision and
  artifact reach this ADR inherits).
- ADR 0009 — remote execution mechanics this extends: pull runners, registry images,
  detach→attach; the cluster is "a host" with a different attach prefix.
- ADR 0005 — composed images; the registry becomes mandatory; DevPod's devcontainer overlap.
- ADR 0007 — per-repo secrets, realized as Kubernetes Secrets per cluster.
- ADR 0010/0011 — provisioning; the per-task clone moves to an init container + PVC.
- ADR 0006 — task service stays the sole DB authority; the CRD (B2) is a projection, never
  a writer.
- The M3 press release (`docs/milestones/M3-remote-execution-environment/press-release.md`
  on the `milestones/M3-remote-execution-environments` branch) — the operator +
  managed-compute promise this serves.
