# 0014 — A workflow can run its tasks as an agent-operator Agent (Kubernetes Jobs)

- Status: Accepted
- Date: 2026-08-03
- Deciders: Nicholas Romero
- Related: ADR 0004 (the workflow abstraction), ADR 0005 (composable images), ADR 0008 (execution
  backends / the `Runner` seam), ADR 0011 (per-task clone provisioning),
  [`agent-operator`](https://github.com/ai-outfitter/agent-operator) OPR-003/004/005

## Context

A panopticon task runs in a container on the operator's own machine, with the operator's Docker
daemon, the operator's secrets file, and the operator's identity. That is the right default for work
you are watching. It is the wrong one for work you want to **delegate**: there is no boundary around
what a task may spend, no identity of its own for it to act as, and no place for it to run when your
laptop is shut.

agent-operator already reconciles exactly that boundary. Its cluster-scoped `Agent` custom resource
produces, per agent:

- a namespace `agent-<name>`;
- an `agent-runtime` ServiceAccount bound to the built-in `admin` ClusterRole **scoped to that
  namespace** — admin inside, nothing outside;
- an `agent-workspace` `ResourceQuota` and a `LimitRange`, a hard budget nothing in the namespace
  can widen;
- the Secrets and ConfigMaps named by `spec.credentials`, projected as env or as mounts — the
  agent's own credentials, e.g. its own GitHub account, not the operator's.

Three things agent-operator does **not** provide today, checked against its Go rather than its
docs, and which panopticon therefore does itself:

1. **No launch API.** OPR-005 (subagent Jobs) and the proposed `Run` CRD are design-only; there is
   no Job code in the operator. The one sanctioned path is that the namespace's admin rights let a
   Job be created there — by the agent, "or the operator on the agent's behalf" (OPR-005.1).
2. **The image is panopticon's.** The agent's runtime image (`spec.image`) carries Outfitter and the
   agent's composition; it has no panopticon package, and panopticon's in-container half (the
   entrypoint, the agent launcher, the hooks) is what makes a task a task.
3. **The workspace PVC is not available.** `agent-workspace` is ReadWriteOnce and already mounted by
   the agent's always-on Deployment, so a task pod cannot mount it.

## Decision

**A workflow declares the agent it runs as.** `Workflow.runner_type = "kubernetes"` selects the new
backend and `Workflow.operator_agent` names the `Agent`; `KubernetesRunner` creates one
`batch/v1` Job per task in that agent's namespace.

The choice that matters here is *where the binding lives*. It could have been a flag on the host
daemon — one host, one agent. It is on the workflow instead, because which agent should run a piece
of work is a property of the work: a research workflow runs as the research agent, with its catalog
and its mailbox; a review workflow runs as the reviewer, with its GitHub account. One host serves
both, and moving the daemon to another machine changes nothing about who a task acts as.

1. **The `Agent` CR is the configuration.** `sessionservice/agent_operator.py` reads it with
   `kubectl get` and reduces it to what a Job needs: the namespace (`status.namespace` — never a
   guessed `agent-<name>`, which would race the operator's ownership check), the organization and
   profile, and the credentials. Everything the Job gets, the agent already declared; panopticon adds
   no per-task grant of its own. The `Agent` is cluster-scoped, so this read uses the operator's
   kubeconfig — the namespace-scoped `agent-runtime` token could not do it, which is the correct
   asymmetry: panopticon composes the Job from outside, the Job runs from inside.

2. **`kubectl` behind an injectable command runner**, matching how the local runner shells out to
   `docker`/`tmux`. It carries kubeconfig and context selection for free, `kubectl exec` is the
   interactive surface anyway, and it keeps panopticon free of a Kubernetes client dependency.
   Manifests are emitted as JSON, so the whole backend is testable by asserting on argv plus one
   document.

3. **The pod prepares itself.** No host clone is mounted (a pod may land on any node), so
   `container/pod.py` clones `/workspace` — an `emptyDir` — from the repo's git URL, starts the agent
   in an **in-pod** tmux session, and hands off to the ordinary container entrypoint for liveness.
   `kubectl exec -it <pod> -- tmux attach` replaces the host tmux pane. The base image gains `tmux`
   for this.

4. **A raw Job, shaped to be recognizable.** `serviceAccountName: agent-runtime`,
   `restartPolicy: Never`, `backoffLimit: 0` (respawn is the host daemon's own self-heal, so a
   silent Job-controller retry would only hide a failure), `ttlSecondsAfterFinished` so finished
   Jobs do not sit in a bounded quota, and the operator's ownership labels plus `panopticon.task`
   so both sides can see what a Job is. The Job name is deterministic (`panopticon-<task_id>`, the
   shared session convention), so a respawn replaces rather than duplicates. `stop` deletes the
   Job — never the namespace (OPR-005.6).

5. **The pod runs as the image's baked unprivileged user** (uid 1000, `runAsNonRoot`). The pod
   command replaces the image entrypoint, whose only job is a host-uid remap a pod does not need —
   and stating the user makes that a checked choice rather than an accident that leaves the task as
   root.

6. **Host-local secrets do not cross into the cluster.** The repo's `env_file` (ADR 0007) is
   deliberately not passed to a Kubernetes task. Its credentials are its agent's.

7. **A host without the backend fails the spawn.** `--kubernetes` enables it; without it, a
   kubernetes workflow's task reports `failed` rather than falling back to Docker, which would
   silently run it under the operator's identity instead of the agent's — a downgrade of exactly the
   boundary the workflow asked for.

## Consequences

- The determinism invariant is unchanged: the control plane still makes no LLM call, and the agent
  still runs in the spawned container — now a pod.
- Real isolation and a real budget, per agent rather than per host: the namespace `ResourceQuota` is
  a wall panopticon cannot widen. It is shared with the agent's always-on Deployment, so a task
  competes with its own agent for the agent's budget — which is the intended accounting.
- The dependency on agent-operator is thin: a namespace with an admin-scoped ServiceAccount, a
  quota, and declared credentials. The `Agent` CR is how that comes to exist.
- **The pod runs panopticon's agent, not the agent's Outfitter composition.** The `Agent` supplies
  identity, credentials, and budget; the *harness* is panopticon's. Converging those — a Job that
  runs `outfitter run <agent>` against a typed task input — needs Outfitter's headless task contract
  and is deliberately not attempted here.
- **Liveness is the in-pod session, not the pod.** A Docker task's agent dying takes its host tmux
  session with it, which is what the daemon heals on. A pod has no host-side session to lose, so the
  bootstrap holds the liveness connection only while its tmux session exists; otherwise a task whose
  agent died would read `live` and be unattachable. Found by running it (see Validation).
- **`--kubernetes-image` must be a reference the cluster itself resolves.** A bare `panopticon-base`
  is expanded by containerd to `docker.io/library/panopticon-base`, which does not exist. Locally
  that means tagging under `localhost/` before importing; in a real cluster it is a registry path.
- The terminal supervisor's `attach_command` (`terminal/attach.py`) still builds a plain or
  ssh-wrapped `tmux` attach. `KubernetesRunner.attach_command` emits the `kubectl exec` form;
  wiring the supervisor to ask the runner for it is a follow-up, so until then a kubernetes task is
  attached by hand.
- When agent-operator grows the OPR-006 `Run` launch CRD, the runner's `kubectl apply` of a Job
  becomes a `kubectl apply` of a `Run` with the same fields, and the operator owns materialization,
  run history, and concurrency. Nothing above changes shape.

## Validation

`dev/k8s-local.sh` sets this up against agent-operator's own microVM dev cluster: it applies
`dev/k8s-agent.yaml`, seeds the agent's credentials secret, ships the task image into the cluster,
and prints the two commands to run. `dev/workflows/k8s_spike.py` is a `runner_type = "kubernetes"`
workflow to create tasks on.

Run on 2026-08-04 against k3s v1.35.6, observed end to end: the Job created in `agent-panopticon`;
the pod cloning its own `/workspace`; `LINK_AGENT`/`LINK_AGENT_SLUG`/`LINK_ORGANIZATION` and the
agent's credentials reaching the container from the `Agent` CR; the container running as uid 1000;
the pod registering liveness back to the control plane **outside** the cluster (`10.0.2.2:8000`, the
microVM's slirp gateway); `claude` live in the pod's tmux session over `kubectl exec -it … tmux
attach`; and `stop` deleting the Job with the namespace untouched.

Two defects it caught, both now fixed and covered by tests: a `Pending` pod read as "no session", so
the daemon healed every brand-new task into a delete-and-recreate loop; and a dead agent leaving the
pod holding liveness, so a task with no working agent read `live`.
