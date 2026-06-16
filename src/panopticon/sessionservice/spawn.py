"""Spawn-prep (ADR 0011): clone the per-task checkout before launching the container.

Before the runner spawns a task's container, the session service gives it a writable working copy:
it makes the repo's cache clone current (`CloneCache`) and `git clone --local`s it to the per-task
path that gets bind-mounted at ``/workspace``. A ``--local`` clone is self-contained (hardlinked
objects), so it mounts at any container path; the agent works there the whole task and the slug
later just branches it (`Provisioner`).

Idempotent: skips the clone (and the cache fetch) when the per-task checkout already exists — e.g.
a re-created container re-mounts the same dir. LLM-free.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from panopticon.client import JsonObj
from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache


def prepare_workspace(
    task_id: str,
    repo: JsonObj,
    *,
    cache: CloneCache,
    tasks_root: str,
    git: GitClones | None = None,
    exists: Callable[[str], bool] = os.path.isdir,
) -> str:
    """Ensure the task's per-task clone exists and return its path (mount this at ``/workspace``).

    Makes the repo's cache clone current, then ``git clone --local``s it to
    ``<tasks_root>/<task_id>`` if that checkout isn't already there. ``git``/``exists`` are
    injectable so the emitted commands are unit-testable without a real repo.
    """
    git = git or GitClones()
    clone = f"{tasks_root.rstrip('/')}/{task_id}"
    if not exists(clone):
        cache_path = cache.ensure(repo["id"], repo["git_url"])
        git.clone_local(cache_path=cache_path, dest=clone)
    return clone
