"""Host-side task provisioning (ADR 0010): create the slug-named worktree, record it back.

The session service runs **where the container runs**, so it owns the host git. When a task
acquires a slug, this ensures the repo's local clone (`CloneCache`), creates ``panopticon/<slug>``
as a worktree off it, and records the result on the task service (`PUT /tasks/{id}/provisioning`) —
which itself does no filesystem work, so the split stays correct when the runner is remote (ADR
0009). LLM-free (the determinism invariant): pure git + REST.

Provisioning is **observed, not pushed** (ADR 0010): the session service spots the slug over its
work-pull loop (`ProvisionDaemon`) and calls :meth:`Provisioner.provision`. That makes the call
**idempotent** — it no-ops a task with no slug yet or one whose worktree is already recorded — so
the loop can call it on every task it sees without double-creating.

This module is the per-task provisioning step. Repointing the container's read-only checkout at
the new worktree (the agent `cd`s in) is the remaining Slice 7 wiring.
"""

from __future__ import annotations

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitWorktrees, Worktree
from panopticon.sessionservice.clones import CloneCache


class Provisioner:
    """Creates each task's host worktree once it has a slug, and records it on the task service.

    ``clones`` maintains the per-repo local clones a worktree is cut from; ``worktrees_root`` is
    where per-task worktrees are checked out (`core.git.worktree_path`). ``git`` is injectable so
    the emitted commands are unit-testable without a real repo.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        clones: CloneCache,
        *,
        worktrees_root: str,
        git: GitWorktrees | None = None,
    ) -> None:
        self._client = client
        self._clones = clones
        self._worktrees_root = worktrees_root
        self._git = git or GitWorktrees()

    def provision(self, task: JsonObj) -> Worktree | None:
        """Provision ``task`` if it is ready, returning the created worktree (else ``None``).

        Ready means it has a slug but no worktree recorded yet; otherwise this no-ops (idempotent,
        so the pull loop can call it on every task). Ensures the repo's clone, creates
        ``panopticon/<slug>`` off the repo's ``default_base`` from that clone, then records the
        branch/path on the task service.
        """
        if not task.get("slug") or task.get("worktree"):
            return None
        repo_id = task["repo_id"]
        repo = self._client.get_repo(repo_id)
        clone = self._clones.ensure(repo_id, repo["git_url"])
        worktree = self._git.create(
            repo_path=clone,
            worktrees_root=self._worktrees_root,
            repo_id=repo_id,
            slug=task["slug"],
            base=repo["default_base"],
        )
        self._client.record_provisioning(task["id"], worktree.branch, worktree.path)
        return worktree
