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


class GitError(RuntimeError):
    """A ``git`` command that exited non-zero, carrying its ``stderr``.

    Callers that must *interpret* a failure (the session service's publisher classifies a refused
    push into an actionable remedy) need git's message, not just an exit code — and they shouldn't
    have to catch :class:`subprocess.CalledProcessError`, which would tie them to the real runner
    and make test fakes raise something they don't otherwise import. Ops that can fail meaningfully
    raise this instead; the plumbing ops keep raising whatever the runner raises.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


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


#: ``git submodule status`` state characters — the first column of each line.
SUBMODULE_UNINITIALIZED = "-"  # not checked out (no objects, an empty directory)
SUBMODULE_MODIFIED = "+"  # checked out at a commit other than the gitlink's
SUBMODULE_CONFLICTED = "U"  # has merge conflicts
SUBMODULE_CURRENT = " "  # checked out at the recorded commit


def parse_submodule_status(output: str) -> dict[str, str]:
    """Parse ``git submodule status`` output into ``{submodule path: state character}``.

    Each line is ``<state><sha> <path>``, plus a `` (<describe>)`` suffix on an initialized
    submodule. The path is taken as everything between the sha and that suffix rather than by
    field index, so a submodule path containing spaces survives. A line that doesn't match the
    shape is skipped — a parse this thin should ignore what it doesn't recognize, not guess.
    """
    states: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        state, entry = line[0], line[1:]
        _sha, _, path = entry.partition(" ")
        if path.endswith(")"):
            path = path.rpartition(" (")[0] or path
        if path:
            states[path] = state
    return states


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

    def submodule_status(self, *, repo_path: str) -> dict[str, str]:
        """The repo's submodules and their states — ``{path: state}``, empty when there are none.

        Reads ``git -C <repo> submodule status --recursive`` (no network, nothing written) and parses
        it, so callers ask about a submodule instead of slicing git's columns. ``--recursive``
        descends into the submodules that *are* initialized, so a nested one that isn't shows up too.
        The state is one of the :data:`SUBMODULE_UNINITIALIZED`/:data:`SUBMODULE_MODIFIED`/
        :data:`SUBMODULE_CONFLICTED`/:data:`SUBMODULE_CURRENT` characters git puts in the first
        column.
        """
        out = self._run(["git", "-C", repo_path, "submodule", "status", "--recursive"])
        return parse_submodule_status(out)

    def update_submodules(self, *, repo_path: str) -> None:
        """``git -C <repo> submodule update --init --recursive`` — fill in the submodule checkouts.

        ``protocol.file.allow=always`` is **required**, not cosmetic: since git 2.38 a submodule
        whose resolved URL is a local path is refused (``transport 'file' not allowed``,
        CVE-2022-39253), and a local-git repo's ``git_url`` *is* a host path — so relative
        ``.gitmodules`` URLs resolve to local paths and every such task would fail to provision.
        It is set for this one command only (never for the container's git), and stays inside the
        existing trust boundary: the repo is operator-registered and the session service already
        clones it from that same local path.

        Submodule URLs are resolved against the superproject's ``remote.origin.url`` *here*, so the
        caller must point ``origin`` at the forge first (:meth:`set_origin`) — resolving a relative
        URL against the cache path would look for the submodule next to the cache clone.
        """
        self._run(
            [
                "git",
                "-C",
                repo_path,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--recursive",
            ]
        )

    def push(self, *, repo_path: str, remote: str, branch: str) -> None:
        """``git -C <repo> push <remote> <branch>`` — send one branch, as-is (never forced).

        Raises :class:`GitError` with git's ``stderr`` on a rejected push, so the caller can tell a
        refused checked-out branch from a non-fast-forward and act on it. Pushing *from* the
        per-task clone is how a merge reaches a local-filesystem origin the container can't see.
        """
        try:
            self._run(["git", "-C", repo_path, "push", remote, branch])
        except subprocess.CalledProcessError as err:
            raise GitError(
                f"pushing {branch!r} to {remote!r} failed", stderr=err.stderr or ""
            ) from err
