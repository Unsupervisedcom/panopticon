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


class GitClones:
    """Per-task **local clones** — the writable checkout a task works in (ADR 0011).

    A ``git clone --local`` of the repo's cache clone is *self-contained* (its own objects —
    hardlinked from the cache, so creation is near-free on one filesystem — refs, config, HEAD),
    so it mounts at any container path with no symlink or path-mirroring. The task is provisioned
    by **branching whatever's there** once its slug is set, then pointing ``origin`` at the real
    forge (a ``--local`` clone's origin is the cache). Same injectable runner as ``GitWorktrees``.
    """

    def __init__(self, *, run: CommandRunner = _subprocess_run) -> None:
        self._run = run

    def clone_local(self, *, cache_path: str, dest: str) -> None:
        """``git clone --local <cache> <dest>`` — a self-contained checkout (hardlinked objects)."""
        self._run(["git", "clone", "--local", cache_path, dest])

    def create_branch(self, *, repo_path: str, branch: str) -> None:
        """``git -C <repo> checkout -b <branch>`` — branch whatever is checked out (ADR 0011 §2)."""
        self._run(["git", "-C", repo_path, "checkout", "-b", branch])

    def set_origin(self, *, repo_path: str, url: str) -> None:
        """``git -C <repo> remote set-url origin <url>`` — point at the forge, not the cache."""
        self._run(["git", "-C", repo_path, "remote", "set-url", "origin", url])

    def configure_safe_directory(self) -> None:
        """Allow git operations on any path regardless of ownership (idempotent)."""
        self._run(["git", "config", "--global", "--add", "safe.directory", "*"])
