"""Spawn-prep (ADR 0011): clone the per-task checkout before launching the container.

Before the runner spawns a task's container, the session service gives it a writable working copy:
it makes the repo's cache clone current (`CloneCache`) and `git clone --local`s it to the per-task
path that gets bind-mounted at ``/workspace``. A ``--local`` clone is self-contained (hardlinked
objects), so it mounts at any container path; the agent works there the whole task and the slug
later just branches it (`Provisioner`). Submodules are filled in too — *after* ``origin`` is
repointed, since that's what relative ``.gitmodules`` URLs resolve against, and **from the repo's
own checkout on this host** when ``git_url`` names one (`hydrate_submodules`), so they're local
hardlink clones rather than a per-task fetch from their forge.

Idempotent: skips the clone (and the cache fetch) when the per-task checkout already exists — e.g.
a re-created container re-mounts the same dir. LLM-free.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from panopticon.client import JsonObj
from panopticon.core.git import SUBMODULE_UNINITIALIZED, GitClones, local_repo_path
from panopticon.sessionservice.clones import CloneCache

_log = logging.getLogger(__name__)

#: Suffix appended to a per-task checkout the daemon cannot fully delete (see
#: :func:`cleanup_workspace`). Task ids contain no dots, so a quarantined dir can never
#: collide with another task's checkout path.
QUARANTINE_SUFFIX = ".stale"

#: How deep :func:`hydrate_submodules` follows nested submodules. A guard against a cyclic or
#: pathological nesting, not a real limit — git itself becomes unusable long before this.
MAX_SUBMODULE_DEPTH = 10


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

    Finally fills in the repo's **submodules** if any are still uninitialized (see
    :func:`_needs_submodules`): a ``--local`` clone carries the gitlinks and ``.gitmodules`` but no
    submodule objects, so without this the agent gets empty submodule directories. It runs *after*
    the ``set-url`` because relative submodule URLs (``../lib.git``) are resolved against the
    superproject's ``origin``, which must therefore already be the forge. A repo with no submodules
    pays one ``submodule status`` (empty output, no network); ``git`` records the submodule gitdir
    and worktree links **relatively**, so the checkout still mounts at any container path.
    """
    git = git or GitClones()
    clone = f"{tasks_root.rstrip('/')}/{task_id}"
    if not exists(clone):
        makedirs(str(Path(clone).parent))
        cache_path = cache.ensure(repo["id"], repo["git_url"])
        git.clone_local(cache_path=cache_path, dest=clone)
    git.set_origin(repo_path=clone, url=repo["git_url"])
    if _needs_submodules(git.submodule_status(repo_path=clone)):
        _fill_in_submodules(clone, repo, git=git, exists=exists)
    return clone


def _fill_in_submodules(
    clone: str, repo: JsonObj, *, git: GitClones, exists: Callable[[str], bool]
) -> None:
    """Check the repo's submodules out into the per-task clone — locally when that's possible.

    Prefers :func:`hydrate_submodules` from the repo's own checkout on this host (a local ``git_url``
    — the donor), and otherwise, or if that leaves anything uninitialized, falls back to the plain
    fetch-from-their-URLs update. The donor path is **only** an optimisation: any way it can fail
    ends in exactly the behaviour a repo without a donor gets.
    """
    donor = local_repo_path(str(repo["git_url"]))
    if donor and exists(donor):
        try:
            hydrate_submodules(clone, donor, git=git, exists=exists)
        except Exception:  # any donor failure falls back to the network path below
            _log.warning(
                "hydrating %s's submodules from %s failed; fetching them instead",
                clone,
                donor,
                exc_info=True,
            )
        else:
            if not _needs_submodules(git.submodule_status(repo_path=clone)):
                return
            _log.info(
                "%s still has uninitialized submodules after hydrating from %s; fetching them",
                clone,
                donor,
            )
    git.update_submodules(repo_path=clone)


def hydrate_submodules(
    clone: str,
    donor: str,
    *,
    git: GitClones,
    exists: Callable[[str], bool] = os.path.isdir,
    depth: int = MAX_SUBMODULE_DEPTH,
) -> None:
    """Check ``clone``'s submodules out by cloning them from ``donor``'s hydrated ones.

    ``donor`` is the repo's own checkout on this host (a local ``git_url``), which already holds
    every submodule's objects — so each submodule is a *local* clone (hardlinked object store,
    no network) instead of a fetch from its forge, paid once per task. That is the whole point:
    the superproject was already near-free (``clone --local``); this makes its submodules so too.

    The donor is matched **by path**, never by URL: the donor resolved its relative ``.gitmodules``
    URLs against *its own* ``origin`` and the per-task clone resolves them against the repo's
    ``git_url``, so the two can name the same submodule differently. Per level:

    1. ``submodule init`` — let git resolve the declared URLs into config;
    2. for each submodule the donor actually has checked out, overwrite that resolved URL with the
       donor's path (a submodule the donor lacks keeps its real URL and is simply fetched);
    3. ``submodule update`` for **this level only** — a nested submodule's URL can't be resolved
       before its parent exists;
    4. recurse into each submodule with the matching donor level.

    Then, once at the top, ``submodule sync --recursive`` puts the canonical URLs back — in the
    config *and* in each submodule's own ``origin`` — so the donor's host paths never reach the
    container. Raises whatever ``git`` raises; the caller falls back to the plain update.
    """
    _hydrate_level(clone, donor, git=git, exists=exists, depth=depth)
    git.sync_submodules(repo_path=clone)


def _hydrate_level(
    repo_path: str, donor: str, *, git: GitClones, exists: Callable[[str], bool], depth: int
) -> None:
    """One superproject level of :func:`hydrate_submodules`, then a recursion per submodule."""
    paths = git.submodule_paths(repo_path=repo_path) if depth > 0 else {}
    if not paths:
        return
    git.init_submodules(repo_path=repo_path)
    for name, path in paths.items():
        donor_sub = f"{donor.rstrip('/')}/{path}"
        if exists(donor_sub):
            git.set_submodule_url(repo_path=repo_path, name=name, url=donor_sub)
    git.update_submodules(repo_path=repo_path, recursive=False)
    for path in paths.values():
        _hydrate_level(
            f"{repo_path.rstrip('/')}/{path}",
            f"{donor.rstrip('/')}/{path}",
            git=git,
            exists=exists,
            depth=depth - 1,
        )


def _needs_submodules(states: Mapping[str, str]) -> bool:
    """Whether any of the repo's submodules isn't checked out yet.

    Gated on *uninitialized* rather than on *freshly cloned* for two reasons. A submodule fetch that
    fails transiently leaves the checkout in place, so the clone gate above would skip it forever
    after — here the next spawn pass retries. And on a container re-creation the checkout is the one
    the agent has been working in: an initialized submodule reports modified/conflicted/current even
    when its commit or working tree has moved, so we never run an update that would try to check the
    recorded commit out over the agent's changes.
    """
    return any(state == SUBMODULE_UNINITIALIZED for state in states.values())


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
