"""A cache of each workflow's execution spec — the one place that answers "how does the session
service run this workflow's tasks?".

A workflow's ``runner_type`` (``"docker"``/``"shell"``/``"host"``/``"kubernetes"``), shell ``script``,
``clone_repo``, shell ``workdir`` override, and ``operator_agent`` are static per workflow, so the
session service fetches them once over REST (``GET /workflows/{name}/execution``) and caches them.
Both the :class:`~panopticon.sessionservice.spawner.Spawner` and the
:class:`~panopticon.sessionservice.provisioner.Provisioner` need to know how a workflow runs (spawn
routing, and skip-provisioning respectively); sharing one instance keeps them from drifting.
LLM-free.
"""

from __future__ import annotations

import httpx

from panopticon.client import JsonObj, TaskServiceClient

#: Returned (and cached) when the task service responds 4xx for a workflow name that is no
#: longer in the registry (renamed or removed). Callers see it as a plain docker workflow so
#: cleanup/runner-selection can proceed without raising.
_FALLBACK_SPEC: JsonObj = {
    "runner_type": "docker",
    "script": "",
    "clone_repo": False,
    "workdir": None,
    "operator_agent": None,
}


class WorkflowExecutions:
    """Fetches-once-then-caches each workflow's execution spec (see the module docstring)."""

    def __init__(self, client: TaskServiceClient) -> None:
        self._client = client
        self._specs: dict[str, JsonObj] = {}

    def spec(self, workflow: str) -> JsonObj:
        """The workflow's execution spec (``runner_type``/``script``/``clone_repo``/``workdir``),
        fetched over REST on first use for that workflow, then cached.

        If the task service responds 4xx (e.g. ``UnknownWorkflow`` for a workflow name that was
        renamed or removed), a docker-fallback spec is cached and returned instead of raising.
        This lets terminal tasks with stale workflow names drain (claim released, workspace
        cleaned) rather than poisoning every host tick."""
        if workflow not in self._specs:
            try:
                self._specs[workflow] = self._client.workflow_execution(workflow)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    self._specs[workflow] = _FALLBACK_SPEC
                else:
                    raise
        return self._specs[workflow]

    def is_shell(self, workflow: str | None) -> bool:
        """Whether ``workflow`` runs as a host shell script (no container). ``None``/missing → False
        (the docker default), so callers can pass a task's ``workflow`` field straight through."""
        return self._runner_type(workflow) == "shell"

    def is_host(self, workflow: str | None) -> bool:
        """Whether ``workflow`` runs its agent directly on this machine, with no container.

        ``None``/missing → False, the docker default, matching :meth:`is_shell`. Distinct from
        ``"shell"``, which runs a *script* and no agent: a ``"host"`` task is an ordinary agent task
        that happens to have no image around it, so it clones, holds liveness, and self-heals like a
        container one."""
        return self._runner_type(workflow) == "host"

    def is_kubernetes(self, workflow: str | None) -> bool:
        """Whether ``workflow`` runs as a Job in an agent-operator ``Agent``'s namespace (ADR 0014).

        ``None``/missing → False, the docker default, matching :meth:`is_shell`."""
        return self._runner_type(workflow) == "kubernetes"

    def operator_agent(self, workflow: str) -> str:
        """The agent-operator ``Agent`` a ``"kubernetes"`` workflow runs as.

        Raises ``ValueError`` when the workflow declares none. The workflow class already refuses to
        be defined that way, so reaching this means the task service and this host disagree about
        the registry — worth failing loudly rather than spawning into a guessed namespace."""
        agent = self.spec(workflow).get("operator_agent")
        if not agent:
            raise ValueError(f"kubernetes workflow {workflow!r} declares no operator_agent")
        return str(agent)

    def _runner_type(self, workflow: str | None) -> str:
        return str(self.spec(workflow)["runner_type"]) if workflow else "docker"
