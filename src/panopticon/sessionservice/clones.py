"""Per-repo clone cache (ADR 0010): one local clone per repo on each session-service host.

A task's worktree is added off the repo's **local clone** (`Provisioner` → `core.git`), so each
host the session service runs on keeps one clone per repo and reuses it across that repo's tasks.
This is the host-side counterpart to the worktree ops: it shells out to ``git`` behind the same
injectable command-runner, so it's unit-testable without a real remote, and LLM-free.

``ensure`` is **idempotent**: it clones on first use and otherwise fetches to keep the base
branch current for read-only planning, then returns the clone path either way — so the daemon's
pull loop can call it for every task it provisions.

Concurrency across a repo's tasks, disk accounting, and GC are deferred (docs/BACKLOG.md);
M1 is single-host, one clone reused serially.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from panopticon.core.git import CommandRunner, _subprocess_run


class CloneCache:
    """Maintains one local clone per repo under ``root`` (``<root>/<repo_id>``).

    ``run`` (the ``git`` executor) and ``exists`` (the on-disk check) are injectable so the
    emitted commands and the clone-vs-fetch decision are unit-testable without a real repo.
    """

    def __init__(
        self,
        root: str,
        *,
        run: CommandRunner = _subprocess_run,
        exists: Callable[[str], bool] = os.path.isdir,
        makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    ) -> None:
        self._root = root.rstrip("/")
        self._run = run
        self._exists = exists
        self._makedirs = makedirs

    def path(self, repo_id: str) -> str:
        """Where this repo's clone lives — ``<root>/<repo_id>`` (the worktree base)."""
        return f"{self._root}/{repo_id}"

    def ensure(self, repo_id: str, git_url: str) -> str:
        """Ensure the repo's clone exists and is current, returning its path. Idempotent.

        Clones from ``git_url`` on first use; on later calls fetches (``--all --prune``) **and
        fast-forwards the checked-out base branch** to its upstream, so the branch a per-task clone
        is cut from is actually current. (``fetch`` alone only moves ``origin/<base>``; the local
        base branch — which ``git clone --local`` copies into the per-task clone — would stay at the
        commit it was first cloned at, so every task would start behind.)
        """
        path = self.path(repo_id)
        if self._exists(path):
            self._run(["git", "-C", path, "fetch", "--all", "--prune"])
            self._run(
                ["git", "-C", path, "merge", "--ff-only"]
            )  # advance the base branch to upstream
        else:
            self._makedirs(self._root)
            self._run(["git", "clone", git_url, path])
        return path
