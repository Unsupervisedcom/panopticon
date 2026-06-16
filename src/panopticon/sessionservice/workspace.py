"""Per-task host workspaces (ADR 0011): the ``repo`` symlink the agent works at.

Each task gets a host dir (``<root>/<task_id>``) mounted into its container at ``/workspace``
(ADR 0011 §1). Inside it, ``repo`` is a **symlink the session service owns**: it points at the
read-only base checkout while the agent plans, and is repointed to the task's writable worktree
once provisioned (ADR 0011 §2). The agent always works at the one stable path ``/workspace/repo``
and `cd`s in itself *after* it observes provisioning — the host never moves a live process's cwd.

The repoint is an **atomic symlink swap** (write a temp link, ``os.replace`` over the old one),
and **idempotent** (a no-op when ``repo`` already points where asked) so the daemon's pull loop
can reconcile it every pass. The dir also holds the per-task agent config dir (``.agent``), which
survives container re-creation so ``claude --continue`` resumes the task (ADR 0011 §5). LLM-free.
"""

from __future__ import annotations

import os


class TaskWorkspaces:
    """Manages per-task workspace dirs under ``root`` (the host ``<root>/<task_id>`` tree)."""

    def __init__(self, root: str) -> None:
        self._root = root.rstrip("/")

    def workspace(self, task_id: str) -> str:
        """The per-task dir mounted at ``/workspace`` — holds the ``repo`` symlink + config dir."""
        return f"{self._root}/{task_id}"

    def repo_link(self, task_id: str) -> str:
        """The ``repo`` symlink the agent works at (``/workspace/repo`` in the container)."""
        return f"{self.workspace(task_id)}/repo"

    def config_dir(self, task_id: str) -> str:
        """The per-task agent config dir (persists across container re-creation; ADR 0011 §5)."""
        return f"{self.workspace(task_id)}/.agent"

    def prepare(self, task_id: str, *, base: str) -> str:
        """Create the task's workspace pointed at the read-only ``base`` checkout (pre-slug).

        Idempotent: re-preparing just re-points ``repo`` at ``base``. Returns the ``repo`` path.
        """
        os.makedirs(self.config_dir(task_id), exist_ok=True)
        self._point(self.repo_link(task_id), base)
        return self.repo_link(task_id)

    def repoint(self, task_id: str, target: str) -> None:
        """Point the task's ``repo`` at ``target`` (the worktree, once provisioned). Idempotent."""
        link = self.repo_link(task_id)
        if os.path.islink(link) and os.readlink(link) == target:
            return  # already there — the loop can reconcile every pass for free
        self._point(link, target)

    @staticmethod
    def _point(link: str, target: str) -> None:
        # Swap atomically: a stale target should never be observable. Write a temp symlink beside
        # the link, then rename it over — `os.replace` is atomic on the same filesystem.
        tmp = f"{link}.tmp"
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.remove(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, link)
