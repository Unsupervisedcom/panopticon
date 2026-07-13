"""Local git: branch naming and per-task clone management — workflow-agnostic **core** ops (ADR 0004).

ADR 0004 puts *local* git in the core, agnostic of any workflow; only *remote* forge
integration (PR/CI/merge) is workflow-specific.  It shells out to `git` behind an **injectable
command-runner** so it's unit-testable without a real repo, and LLM-free. It is the one
I/O-bearing module in `core`; the domain models and state machine stay pure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
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


class GitClones:
    """Per-task **local clones** — the writable checkout a task works in (ADR 0011).

    A ``git clone --local`` of the repo's cache clone is *self-contained* (its own objects —
    hardlinked from the cache, so creation is near-free on one filesystem — refs, config, HEAD),
    so it mounts at any container path with no symlink or path-mirroring. The task is provisioned
    by **branching whatever's there** once its slug is set, then pointing ``origin`` at the real
    forge (a ``--local`` clone's origin is the cache).
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
