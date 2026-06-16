"""Host-side task provisioning (ADR 0011): branch the per-task clone, record it back.

The session service runs **where the container runs**, so it owns the host git. Each task works in
a writable per-task ``git clone --local`` created at spawn (a self-contained checkout under
``<clones_root>/<task_id>``, mounted at ``/workspace``). When the agent acquires a slug, this
**branches whatever's there** — ``git checkout -b panopticon/<slug>`` — points ``origin`` at the
repo's real forge (a ``--local`` clone's origin is the cache), and records ``(branch, clone path)``
on the task service (`PUT /tasks/{id}/provisioning`). The task service does no filesystem work, so
the split stays correct when the runner is remote (ADR 0009). LLM-free: pure git + REST.

Provisioning is **observed, not pushed** (ADR 0010): the session service spots the slug over its
work-pull loop (`ProvisionDaemon`) and calls :meth:`Provisioner.provision`. The call is
**idempotent** — it no-ops a task with no slug yet or one already branched — so the loop can call it
on every task it sees. There is no worktree, no symlink, and no repoint: the agent keeps working in
the same ``/workspace``, now on its feature branch (ADR 0011 §2/§3).
"""

from __future__ import annotations

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones, branch_name


class Provisioner:
    """Branches each task's per-task clone once it has a slug, and records it on the task service.

    ``clones_root`` holds the per-task clones (``<clones_root>/<task_id>``, created at spawn-prep and
    mounted at ``/workspace``). ``git`` is injectable so the emitted commands are unit-testable
    without a real repo.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        clones_root: str,
        git: GitClones | None = None,
    ) -> None:
        self._client = client
        self._clones_root = clones_root.rstrip("/")
        self._git = git or GitClones()

    def provision(self, task: JsonObj) -> str | None:
        """Provision ``task`` if it is ready, returning the created branch (else ``None``).

        Ready means it has a slug but isn't branched yet; otherwise this no-ops (idempotent, so the
        pull loop can call it on every task). Branches the per-task clone off its current HEAD,
        points ``origin`` at the repo's forge, then records the branch + clone path on the task
        service.
        """
        if not task.get("slug") or task.get("branch"):
            return None
        clone = f"{self._clones_root}/{task['id']}"
        branch = branch_name(task["slug"])
        self._git.create_branch(repo_path=clone, branch=branch)
        self._git.set_origin(repo_path=clone, url=self._client.get_repo(task["repo_id"])["git_url"])
        self._client.record_provisioning(task["id"], branch, clone)
        return branch
