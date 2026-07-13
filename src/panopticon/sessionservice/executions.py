"""A cache of each workflow's execution spec — the one place that answers "how does the session
service run this workflow's tasks?".

A workflow's ``runner_type`` (``"docker"``/``"shell"``), shell ``script``, ``clone_repo``, and shell
``workdir`` override are static per workflow, so the session service fetches them once over REST
(``GET /workflows/{name}/execution``) and caches them. Both the :class:`~panopticon.sessionservice.
spawner.Spawner` and the :class:`~panopticon.sessionservice.provisioner.Provisioner` need "is this a
shell workflow?" (routing, and skip-provisioning respectively); sharing one instance keeps them from
drifting. LLM-free.
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
        if not workflow:
            return False
        return bool(self.spec(workflow)["runner_type"] == "shell")
