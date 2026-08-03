"""Resolve an `agent-operator <https://github.com/ai-outfitter/agent-operator>`_ ``Agent`` into
the pieces a task Job needs (ADR 0014).

agent-operator reconciles a cluster-scoped ``Agent`` custom resource into a bounded, credentialed
workspace: a namespace ``agent-<name>``, a ``agent-runtime`` ServiceAccount bound to the built-in
``admin`` ClusterRole **scoped to that namespace**, a ``ResourceQuota``/``LimitRange`` pair, the
agent's durable PVCs, and the Secrets/ConfigMaps its ``spec.credentials`` name. panopticon does not
own any of that — it *references* an existing ``Agent`` and spawns task Jobs into its namespace,
under its identity and inside its budget.

This module is the one place that knows the operator's resource conventions, so a change there is a
change here and nowhere else. The names below are mirrored from the operator's
``internal/controller/agent_resources.go``; keep them in step.

We read the ``Agent`` with ``kubectl get`` behind an injectable command-runner — the same convention
the local runner uses for ``docker``/``tmux``, and enough to stay dependency-free (no Kubernetes
client library). ``Agent`` is **cluster-scoped**, so this read uses the operator's own kubeconfig,
not the in-namespace ``agent-runtime`` token; the Job it produces is what runs under that token.
LLM-free.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from panopticon.sessionservice.kubectl import CommandRunner, kubectl_argv

#: The custom resource panopticon reads. Cluster-scoped, so it is fetched without ``--namespace``.
AGENT_RESOURCE = "agents.link.aioutfitter.com"

#: The ServiceAccount agent-operator provisions in every agent namespace, bound to the built-in
#: ``admin`` ClusterRole scoped to that namespace. Task Jobs run as it — that binding is what lets
#: a task pod act inside its own namespace, and nothing outside it.
RUNTIME_SERVICE_ACCOUNT = "agent-runtime"

#: Where the operator mounts an agent's volume-exposed credentials. Mirrored so a task pod finds a
#: credential at the same path the always-on agent does.
CREDENTIALS_ROOT = "/var/run/link/credentials"

#: Label the operator puts on everything it owns in an agent namespace. panopticon sets it on task
#: Jobs too, so the agent (and the operator) recognize a Job as belonging to that agent.
AGENT_LABEL = "link.aioutfitter.com/agent"


@dataclass(frozen=True)
class Credential:
    """One ``spec.credentials`` entry, resolved to how it reaches a pod."""

    #: ``"Secret"`` or ``"ConfigMap"`` — the object kind holding the values.
    kind: str
    name: str
    #: ``"env"`` (the whole object becomes environment variables) or ``"volume"`` (mounted under
    #: :data:`CREDENTIALS_ROOT`).
    exposure: str

    @property
    def volume_name(self) -> str:
        """The pod volume name, matching the operator's ``credentialVolumeName``."""
        prefix = "secret-" if self.kind == "Secret" else "config-"
        return (prefix + self.name)[:63].rstrip("-")

    @property
    def mount_path(self) -> str:
        """Where a ``"volume"`` credential is mounted, matching the operator's projection."""
        return f"{CREDENTIALS_ROOT}/{self.kind.lower()}s/{self.name}"


@dataclass(frozen=True)
class AgentWorkspace:
    """A resolved agent-operator ``Agent`` — everything a task Job needs to run as that agent.

    Deliberately *not* the whole CR: only the fields that shape a Job. Anything else the agent owns
    (its always-on Deployment, its PVCs, its browser sidecar) is the operator's business.
    """

    #: The ``Agent`` resource name.
    name: str
    #: Its namespace (``status.namespace``, i.e. ``agent-<name>``) — where task Jobs are created.
    namespace: str
    #: The organization the agent's first membership names. Passed to the pod as ``LINK_ORGANIZATION``.
    organization: str
    #: The Dotagents agent slug and harness the agent is composed from (``spec.profile``). Passed
    #: through as ``LINK_AGENT_SLUG``/``LINK_AGENT_HARNESS`` so the pod can report *which* agent it
    #: is running as; panopticon does not resolve the composition itself (see ADR 0014).
    agent_slug: str
    harness: str
    #: The credentials the operator projects onto the agent's own pods; a task Job gets the same set.
    credentials: tuple[Credential, ...] = field(default_factory=tuple)

    @property
    def labels(self) -> dict[str, str]:
        """The operator-recognizable ownership labels for a resource panopticon creates here."""
        return {AGENT_LABEL: self.name, "app.kubernetes.io/managed-by": "panopticon"}


class UnknownAgent(Exception):
    """Raised when the named ``Agent`` does not exist, or the operator has not reconciled it yet."""


def _credentials(spec: dict[str, Any]) -> tuple[Credential, ...]:
    resolved = []
    for reference in spec.get("credentials") or []:
        if name := reference.get("secret"):
            kind = "Secret"
        elif name := reference.get("configMap"):
            kind = "ConfigMap"
        else:  # the CRD's XValidation makes this unreachable in a real cluster
            continue
        resolved.append(Credential(kind=kind, name=name, exposure=reference.get("as", "env")))
    return tuple(resolved)


def resolve_agent(
    name: str,
    *,
    run: CommandRunner,
    kubectl: Sequence[str] = ("kubectl",),
    context: str | None = None,
) -> AgentWorkspace:
    """Read the ``Agent`` named ``name`` and reduce it to an :class:`AgentWorkspace`.

    Raises :class:`UnknownAgent` when the resource is missing, or when the operator has not yet
    written ``status.namespace`` — a not-yet-reconciled agent has no namespace to spawn into, and
    guessing ``agent-<name>`` would race the operator's own ownership check.
    """
    argv = kubectl_argv(kubectl, context, "get", AGENT_RESOURCE, name, "--output", "json")
    output = run(argv, check=False)
    if not output.strip():
        raise UnknownAgent(f"no agent-operator Agent named {name!r} (is it applied?)")
    agent = json.loads(output)
    spec = agent.get("spec") or {}
    namespace = (agent.get("status") or {}).get("namespace")
    if not namespace:
        raise UnknownAgent(
            f"Agent {name!r} has no status.namespace yet — the operator has not reconciled it"
        )
    memberships = spec.get("memberships") or [{}]
    return AgentWorkspace(
        name=name,
        namespace=namespace,
        organization=memberships[0].get("organization", ""),
        agent_slug=(spec.get("profile") or {}).get("agent", ""),
        harness=(spec.get("profile") or {}).get("harness", ""),
        credentials=_credentials(spec),
    )
