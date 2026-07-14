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

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from panopticon.client import JsonObj
from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache

_log = logging.getLogger(__name__)

#: Suffix appended to a per-task checkout the daemon cannot fully delete (see
#: :func:`cleanup_workspace`). Task ids contain no dots, so a quarantined dir can never
#: collide with another task's checkout path.
QUARANTINE_SUFFIX = ".stale"


def prepare_workspace(
    task_id: str,
    repo: JsonObj,
    *,
    cache: CloneCache,
    tasks_root: str,
    git: GitClones | None = None,
    exists: Callable[[str], bool] = os.path.isdir,
    makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
) -> str:
    """Ensure the task's per-task clone exists and return its path (mount this at ``/workspace``).

    Makes the repo's cache clone current, then ``git clone --local``s it to
    ``<tasks_root>/<task_id>`` if that checkout isn't already there. ``git``/``exists`` are
    injectable so the emitted commands are unit-testable without a real repo.

    Then points ``origin`` at the repo's forge — its ``git_url``, used **verbatim** (a ``--local``
    clone's origin is the cache *path*, which the container can neither push to nor let ``gh``
    resolve, so it would fork to the token's own account). The ``git_url`` is registered in the form
    the container should use as its remote — HTTPS for token auth, SSH for key auth — so no rewriting
    happens here. Done at spawn, not deferred to slug-time provisioning, so the agent has a correct
    ``origin`` from its first action; ``set-url`` is idempotent, so it also repoints an existing clone.
    """
    git = git or GitClones()
    clone = f"{tasks_root.rstrip('/')}/{task_id}"
    if not exists(clone):
        makedirs(str(Path(clone).parent))
        cache_path = cache.ensure(repo["id"], repo["git_url"])
        git.clone_local(cache_path=cache_path, dest=clone)
    git.set_origin(repo_path=clone, url=repo["git_url"])
    return clone


def cleanup_workspace(
    task_id: str,
    tasks_root: str,
    *,
    exists: Callable[[str], bool] = os.path.isdir,
    rmtree: Callable[[str], None] = shutil.rmtree,
    docker_cleanup: Callable[[str], None] | None = None,
    rename: Callable[[str, str], None] = os.rename,
) -> None:
    """Remove the per-task checkout if it exists. Idempotent: no-op when already gone.

    A checkout can hold files the daemon **cannot** delete — e.g. root-owned ``.mypy_cache``/
    ``.pytest_cache`` written by a container process that ran as root (before the entrypoint's
    uid remap, or via ``docker_in_docker``). On a failed delete, three escalating recovery
    attempts are made:

    1. If ``docker_cleanup`` is provided, run it to empty the directory via a throwaway root
       container (same image that created the files, so it has the right privileges), then
       retry ``rmtree`` on the now-empty dir.
    2. If that also fails (or ``docker_cleanup`` is absent), **quarantine** the checkout:
       rename it to ``<checkout>.stale`` (a rename needs only write on ``tasks_root``, which
       the daemon owns) and log once for manual removal.
    3. If even the rename fails, log and swallow — cleanup is best-effort; it must never take
       down the host pass.

    Either way the canonical path ends up gone, so the self-gate (``exists``) makes later
    passes a no-op."""
    checkout = f"{tasks_root.rstrip('/')}/{task_id}"
    if not exists(checkout):
        return
    try:
        rmtree(checkout)
        return
    except OSError:
        pass
    if docker_cleanup is not None:
        try:
            docker_cleanup(checkout)
            rmtree(checkout)
            return
        except OSError:
            pass
    quarantine = f"{checkout}{QUARANTINE_SUFFIX}"
    try:
        rename(checkout, quarantine)
    except OSError:
        _log.warning(
            "workspace %s could not be removed or quarantined — remove it manually"
            " (it likely holds files owned by another user; may need sudo)",
            checkout,
            exc_info=True,
        )
        return
    _log.warning(
        "workspace %s holds files the daemon cannot delete (e.g. root-owned caches);"
        " quarantined it as %s — remove it manually (may need sudo)",
        checkout,
        quarantine,
    )
