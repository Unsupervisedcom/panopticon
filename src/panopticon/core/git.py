"""Local git: branch + worktree management — workflow-agnostic **core** ops (ADR 0004).

ADR 0004 puts *local* git (branch creation/naming, worktrees, teardown tiers) in the core,
agnostic of any workflow; only *remote* forge integration (PR/CI/merge) is workflow-specific.
This module is that core capability.

It shells out to `git` behind an **injectable command-runner** (the same pattern as the
docker/tmux runner) so it's unit-testable without a real repo, and LLM-free. It is the one
I/O-bearing module in `core`; the domain models and state machine stay pure.

The branch and worktree are named from the task **slug** (`panopticon/<slug>`,
`<root>/<repo>/<branch>`, refining cloude-cade's `cloude/<slug>`), so creation is **slug-gated**
— it cannot run before the agent has set the slug (ARCHITECTURE §8.3/§9).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

#: Feature-branch namespace (PARITY §8/§14, renamed from cloude-cade's ``cloude/``).
BRANCH_PREFIX = "panopticon"


class CommandRunner(Protocol):
    """Runs an external command and returns its stdout; ``check`` raises on non-zero exit."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def branch_name(slug: str) -> str:
    """The feature branch for a task slug — ``panopticon/<slug>``."""
    return f"{BRANCH_PREFIX}/{slug}"


def worktree_path(worktrees_root: str, repo_id: str, branch: str) -> str:
    """Where a task's worktree lives — ``<root>/<repo>/<branch>`` (PARITY §8)."""
    return f"{worktrees_root.rstrip('/')}/{repo_id}/{branch}"


@dataclass(frozen=True)
class Worktree:
    """A created worktree: its branch and on-disk path."""

    branch: str
    path: str


class GitWorktrees:
    """Create/remove per-task git worktrees on a local repo (one host)."""

    def __init__(self, *, run: CommandRunner = _subprocess_run) -> None:
        self._run = run

    def create(
        self, *, repo_path: str, worktrees_root: str, repo_id: str, slug: str | None, base: str
    ) -> Worktree:
        """Create the slug-named feature branch + worktree off ``base``. Slug-gated.

        Raises :class:`ValueError` if the task has no slug yet — the worktree is named from it,
        so it cannot precede it (and neither can any workflow provisioning that needs the branch).
        """
        if not slug:
            raise ValueError("cannot create a worktree before the task's slug is set")
        branch = branch_name(slug)
        path = worktree_path(worktrees_root, repo_id, branch)
        # `worktree add -b <branch> <path> <base>` creates the branch and checks it out there.
        self._run(["git", "-C", repo_path, "worktree", "add", "-b", branch, path, base])
        return Worktree(branch=branch, path=path)

    def remove(self, *, repo_path: str, worktree_path: str, force: bool = False) -> None:
        """Remove a worktree. ``force`` is the teardown tier that discards uncommitted changes
        (PARITY §8's ``--force-worktree``). Idempotent: tolerates an already-gone worktree."""
        args = ["git", "-C", repo_path, "worktree", "remove", worktree_path]
        if force:
            args.append("--force")
        self._run(args, check=False)
