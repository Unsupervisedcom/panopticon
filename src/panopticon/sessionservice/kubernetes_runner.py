"""Kubernetes runner (ADR 0014): run a task as a Job in an agent-operator ``Agent`` namespace.

The third execution backend, beside :class:`~panopticon.sessionservice.local_runner.LocalRunner`
(Docker + host tmux) and :class:`~panopticon.sessionservice.shell_runner.ShellRunner` (a host shell).
A workflow selects it with ``runner_type = "kubernetes"`` and names the ``Agent`` it runs as with
:attr:`~panopticon.core.workflow.Workflow.operator_agent`, so **which agent runs a task is a
property of the workflow**, not of the host the daemon happens to run on.

What the agent supplies, and what panopticon supplies:

* **agent-operator** — the namespace, the ``agent-runtime`` ServiceAccount (namespace ``admin``),
  the ``ResourceQuota``/``LimitRange`` the Job cannot widen, and the Secrets/ConfigMaps of
  ``spec.credentials``. The task runs under *that agent's* identity and inside *its* budget.
* **panopticon** — the image (its own task image: the base layer plus the workflow/repo layers,
  which is what carries the in-container entrypoint and agent launcher), the Job, and the control
  plane. See :mod:`panopticon.sessionservice.agent_operator` for the resolution step.

Two things the pod does differently from a Docker task, both because there is no host to lean on:
it **clones its own workspace** into an ``emptyDir`` (the agent's ``agent-workspace`` PVC is
ReadWriteOnce and already mounted by the agent's always-on Deployment), and it starts the agent in
an **in-pod** tmux session, so ``kubectl exec -it <pod> -- tmux attach`` is the interactive surface
where a Docker task uses a host tmux pane. See :mod:`panopticon.container.pod`.

Manifests are emitted as JSON (``kubectl apply`` accepts it) and piped to ``kubectl apply -f -``, so
the whole backend is testable by asserting on argv plus one JSON document. LLM-free — the agent runs
in the spawned pod, which is the determinism invariant restated for this backend.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.agent_operator import (
    RUNTIME_SERVICE_ACCOUNT,
    AgentWorkspace,
    resolve_agent,
)
from panopticon.sessionservice.kubectl import CommandRunner, kubectl_argv, subprocess_run
from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, WORKSPACE_MOUNT, session_name
from panopticon.sessionservice.runner import Runner

#: What the pod runs: the in-pod bootstrap (clone → tmux agent session → liveness). Set as the
#: container ``command`` so it replaces the image's entrypoint, whose only job is a host-uid remap
#: that a pod does not need (nothing is bind-mounted from a host).
POD_COMMAND: tuple[str, ...] = ("python", "-m", "panopticon.container.pod")

#: The uid/gid of the image's baked ``panopticon`` user. A pod has no host user to adopt, so the
#: task runs as that account directly — and ``runAsNonRoot`` makes the absence of the remapping
#: entrypoint a deliberate, checked choice rather than an accident that leaves the task as root.
POD_USER = 1000

#: Delete a finished Job (and its pod) this long after it ends. The agent namespace has a hard
#: ``ResourceQuota``, so finished Jobs must not accumulate in it.
DEFAULT_TTL_SECONDS = 3600

#: Pod phases that mean the task is alive. ``Pending`` is included deliberately — see
#: :meth:`KubernetesRunner.is_running`. ``Succeeded``/``Failed`` (and no pod at all) mean it is not.
LIVE_POD_PHASES = frozenset({"Pending", "Running"})

#: Marks a Job as this task's. The label a human greps for, and how :meth:`KubernetesRunner.stop`
#: and the liveness probes find the Job without keeping cluster state locally.
TASK_LABEL = "panopticon.task"


class KubernetesRunner(Runner):
    """Runs task Jobs in an agent-operator ``Agent``'s namespace (one cluster, many agents)."""

    def __init__(
        self,
        service_url: str,
        *,
        image: str = DEFAULT_IMAGE,
        runner_id: str = "local",
        image_pull_policy: str = "IfNotPresent",
        kubectl: Sequence[str] = ("kubectl",),
        context: str | None = None,
        active_deadline_seconds: int | None = None,
        ttl_seconds_after_finished: int | None = DEFAULT_TTL_SECONDS,
        pod_command: Sequence[str] = POD_COMMAND,
        extra_env: Mapping[str, str] | None = None,
        run: CommandRunner = subprocess_run,
    ) -> None:
        #: How the **pod** reaches the task service. The control plane stays wherever it already
        #: runs (usually the operator's own machine); only task pods move into the cluster, so this
        #: is the cluster-side view of the service, not the daemon's own ``--service-url``.
        self._service_url = service_url
        self._image = image
        self._runner_id = runner_id
        #: ``IfNotPresent`` by default so a locally-imported image (``k3s ctr images import`` /
        #: ``kind load`` — the dev path) is used as-is. A ``:latest`` tag would otherwise default to
        #: ``Always`` and fail on a cluster with no registry holding it.
        self._image_pull_policy = image_pull_policy
        self._kubectl = list(kubectl)
        self._context = context
        #: A hard wall-clock limit on a task pod, or ``None`` for none. Distinct from the quota:
        #: the quota bounds what a task may *use*, this bounds how long it may use it.
        self._active_deadline_seconds = active_deadline_seconds
        self._ttl_seconds_after_finished = ttl_seconds_after_finished
        self._pod_command = list(pod_command)
        self._extra_env = dict(extra_env or {})
        self._run = run
        #: Resolved ``Agent``s, keyed by name. An Agent's namespace, identity, and credentials are
        #: stable for the life of the daemon; re-reading the CR on every spawn would add a cluster
        #: round-trip per task for a value that does not move.
        self._agents: dict[str, AgentWorkspace] = {}
        #: task id → the namespace its Job was created in, remembered at spawn so the lifecycle
        #: calls (:meth:`stop`, :meth:`is_running`) need no lookup. A daemon restart empties it and
        #: they fall back to searching the agents it has since resolved.
        self._namespaces: dict[str, str] = {}

    def agent(self, name: str) -> AgentWorkspace:
        """The resolved ``Agent`` named ``name`` (cached). Raises ``UnknownAgent`` if it is absent."""
        if name not in self._agents:
            self._agents[name] = resolve_agent(
                name, run=self._run, kubectl=self._kubectl, context=self._context
            )
        return self._agents[name]

    def _kubectl_argv(self, *args: str, namespace: str | None = None) -> list[str]:
        return kubectl_argv(self._kubectl, self._context, *args, namespace=namespace)

    def spawn(
        self,
        task_id: str,
        *,
        operator_agent: str | None = None,
        git_url: str | None = None,
        image: str | None = None,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str:
        """Create the task's Job in ``operator_agent``'s namespace; return the Job name.

        ``git_url`` is the repo the pod clones into its own ``/workspace`` — unlike the Docker path
        there is no host clone to mount, so the pod does the work (ADR 0011's per-task checkout, one
        layer down). ``image`` overrides the configured task image with the workflow/repo-composed
        one when the caller has built one. ``initial_prompt``, ``turn`` and ``starting_model`` reach
        the agent launcher through the same ``PANOPTICON_*`` variables the Docker runner uses, so
        the in-container half of a task behaves identically on either backend.

        Idempotent: an existing Job of the same name is deleted first, so a respawn replaces rather
        than collides. The name is deterministic (``panopticon-<task_id>``, the shared session
        convention), which is what makes that possible.
        """

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        if operator_agent is None:  # the ABC's signature allows it; this backend cannot
            raise ValueError(f"task {task_id!r}: a kubernetes spawn needs an operator_agent")
        agent = self.agent(operator_agent)
        job = session_name(task_id)
        manifest = self._manifest(
            job,
            task_id,
            agent,
            git_url=git_url,
            image=image,
            initial_prompt=initial_prompt,
            turn=turn,
            starting_model=starting_model,
        )
        # Delete-then-apply rather than a plain apply: a Job's pod template is immutable, so
        # applying over a Job whose template changed (a new image, a new prompt) is rejected.
        self._delete_job(job, agent.namespace, wait=True)
        _report(LifecyclePhase.STARTING)
        self._run(
            self._kubectl_argv("apply", "--filename", "-", namespace=agent.namespace),
            stdin=json.dumps(manifest),
        )
        self._namespaces[task_id] = agent.namespace
        _report(LifecyclePhase.AWAITING)  # Job created; waiting for the pod's /live registration
        return job

    def _manifest(
        self,
        job: str,
        task_id: str,
        agent: AgentWorkspace,
        *,
        git_url: str | None,
        image: str | None,
        initial_prompt: str | None,
        turn: str | None,
        starting_model: str | None,
    ) -> dict[str, Any]:
        """The task Job, as the manifest ``kubectl apply`` receives.

        Split out from :meth:`spawn` because this document *is* the contract with the cluster: a
        test reads it directly, and a reviewer can see the whole pod shape in one place.
        """
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_CONTAINER_ID": job,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            "PANOPTICON_WORKSPACE": WORKSPACE_MOUNT,
            # Which agent-operator Agent this task runs as. The same variables the operator sets on
            # the agent's own Deployment, so anything in the pod that asks "who am I?" gets one
            # answer whether it runs in a task Job or in the always-on agent.
            "LINK_AGENT": agent.name,
            "LINK_AGENT_SLUG": agent.agent_slug,
            "LINK_AGENT_HARNESS": agent.harness,
            "LINK_ORGANIZATION": agent.organization,
            **self._extra_env,
        }
        if git_url:
            env["PANOPTICON_GIT_URL"] = git_url
        if initial_prompt:
            env["PANOPTICON_INITIAL_PROMPT"] = initial_prompt
        if turn:
            env["PANOPTICON_TASK_TURN"] = turn
        if starting_model:
            env["PANOPTICON_STARTING_MODEL"] = starting_model

        env_from = [
            {
                ("secretRef" if credential.kind == "Secret" else "configMapRef"): {
                    "name": credential.name
                }
            }
            for credential in agent.credentials
            if credential.exposure == "env"
        ]
        volume_credentials = [c for c in agent.credentials if c.exposure == "volume"]
        volumes: list[dict[str, Any]] = [{"name": "workspace", "emptyDir": {}}]
        mounts: list[dict[str, Any]] = [{"name": "workspace", "mountPath": WORKSPACE_MOUNT}]
        for credential in volume_credentials:
            source = (
                {"secret": {"secretName": credential.name}}
                if credential.kind == "Secret"
                else {"configMap": {"name": credential.name}}
            )
            volumes.append({"name": credential.volume_name, **source})
            mounts.append(
                {
                    "name": credential.volume_name,
                    "mountPath": credential.mount_path,
                    "readOnly": True,
                }
            )

        labels = {TASK_LABEL: task_id, **agent.labels}
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "serviceAccountName": RUNTIME_SERVICE_ACCOUNT,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": POD_USER,
                "runAsGroup": POD_USER,
                "fsGroup": POD_USER,
            },
            "containers": [
                {
                    "name": "task",
                    "image": image or self._image,
                    "imagePullPolicy": self._image_pull_policy,
                    "command": self._pod_command,
                    "workingDir": WORKSPACE_MOUNT,
                    "env": [{"name": key, "value": value} for key, value in env.items()],
                    **({"envFrom": env_from} if env_from else {}),
                    "volumeMounts": mounts,
                }
            ],
            "volumes": volumes,
        }
        if self._active_deadline_seconds is not None:
            pod_spec["activeDeadlineSeconds"] = self._active_deadline_seconds
        job_spec: dict[str, Any] = {
            # Respawn is the host daemon's own self-heal (it re-detects a down task and spawns
            # again, reporting it), so a silent Job-controller retry would only hide the failure.
            "backoffLimit": 0,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        }
        if self._ttl_seconds_after_finished is not None:
            job_spec["ttlSecondsAfterFinished"] = self._ttl_seconds_after_finished
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job, "namespace": agent.namespace, "labels": labels},
            "spec": job_spec,
        }

    def _delete_job(self, job: str, namespace: str, *, wait: bool = False) -> None:
        """Delete a task's Job. ``wait`` blocks until it is gone — required before re-creating one
        of the same name (a respawn), because an apply is rejected while the old Job terminates."""
        self._run(
            self._kubectl_argv(
                "delete",
                "job",
                job,
                "--ignore-not-found",
                f"--wait={'true' if wait else 'false'}",
                namespace=namespace,
            ),
            check=False,
        )

    def _namespaces_for(self, task_id: str) -> list[str]:
        """Where to look for a task's Job: the namespace it was spawned into if this process spawned
        it, else every agent namespace this runner has resolved.

        The runner is told the agent on :meth:`spawn` but not on :meth:`stop`/:meth:`is_running`. A
        restarted daemon has spawned nothing yet, so it searches instead — and a task it has never
        heard of reads as not running, the same answer a genuinely gone Job gives.
        """
        if namespace := self._namespaces.get(task_id):
            return [namespace]
        return [agent.namespace for agent in self._agents.values()]

    def stop(self, container_id: str) -> None:
        """Delete the task's Job — and only the Job. The agent's namespace is never touched."""
        task_id = container_id.removeprefix("panopticon-")
        for namespace in self._namespaces_for(task_id):
            self._delete_job(container_id, namespace)
        self._namespaces.pop(task_id, None)

    def is_running(self, task_id: str) -> bool:
        """Whether the task's pod is alive in its agent's namespace — scheduling or running.

        Reads the **pod**, not the Job: a Job stays present while its pod fails or completes, so a
        Job-level check would report a dead task as up and defeat the daemon's down-detection.

        ``Pending`` counts as alive (:data:`LIVE_POD_PHASES`). This is the one place the Kubernetes
        backend cannot borrow the local runner's intuition: ``docker run --detach`` returns with the
        container already running, but a pod spends seconds being scheduled and pulling its image.
        Reading that gap as "not running" makes the host daemon see a **brand-new task as an
        orphan** and respawn it — and since respawn deletes and recreates the Job, the task never
        gets far enough to run. A pod that exists and has not finished is a task that is coming up.
        """
        for namespace in self._namespaces_for(task_id):
            phases = self._run(
                self._kubectl_argv(
                    "get",
                    "pod",
                    "--selector",
                    f"{TASK_LABEL}={task_id}",
                    "--output",
                    "jsonpath={.items[*].status.phase}",
                    namespace=namespace,
                ),
                check=False,
            )
            if LIVE_POD_PHASES.intersection(phases.split()):
                return True
        return False

    def has_session(self, task_id: str) -> bool:
        """Whether the task still has a session to attach to.

        A Kubernetes task's tmux session lives **in its pod**, not on the host, so there is no
        host-side session that can be lost while the task keeps running — the orphan case the local
        runner self-heals. Pod liveness is therefore the whole answer.
        """
        return self.is_running(task_id)

    def attach_command(self, task_id: str) -> list[str]:
        """The command that attaches an operator's terminal to the task's agent.

        ``kubectl exec -it`` into the task's pod and attach its in-pod tmux session — the Kubernetes
        equivalent of the local runner's ``tmux attach`` to a host session.
        """
        namespace = next(iter(self._namespaces_for(task_id)), None)
        return self._kubectl_argv(
            "exec",
            "--stdin",
            "--tty",
            f"job/{session_name(task_id)}",
            "--",
            "tmux",
            "attach",
            "-t",
            session_name(task_id),
            namespace=namespace,
        )
