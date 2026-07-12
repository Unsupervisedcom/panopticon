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

from panopticon.client import JsonObj, TaskServiceClient


class WorkflowExecutions:
    """Fetches-once-then-caches each workflow's execution spec (see the module docstring)."""

    def __init__(self, client: TaskServiceClient) -> None:
        self._client = client
        self._specs: dict[str, JsonObj] = {}

    def spec(self, workflow: str) -> JsonObj:
        """The workflow's execution spec (``runner_type``/``script``/``clone_repo``/``workdir``),
        fetched over REST on first use for that workflow, then cached."""
        if workflow not in self._specs:
            self._specs[workflow] = self._client.workflow_execution(workflow)
        return self._specs[workflow]

    def is_shell(self, workflow: str | None) -> bool:
        """Whether ``workflow`` runs as a host shell script (no container). ``None``/missing → False
        (the docker default), so callers can pass a task's ``workflow`` field straight through."""
        if not workflow:
            return False
        return bool(self.spec(workflow)["runner_type"] == "shell")
