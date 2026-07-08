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
from pathlib import Path

from panopticon.client import JsonObj
from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file into a dict, skipping blank lines and ``#`` comments.

    Values are kept verbatim after the first ``=`` so embedded ``=`` characters are preserved.
    Leading/trailing whitespace is stripped from keys only.
    """
    env: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value
    return env


def prepare_workspace(
    task_id: str,
    repo: JsonObj,
    *,
    cache: CloneCache,
    tasks_root: str,
    git: GitClones | None = None,
    exists: Callable[[str], bool] = os.path.isdir,
    makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    parse_env: Callable[[str], dict[str, str]] = _parse_env_file,
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

    If the repo has an ``env_file``, its contents are parsed and forwarded to the cache's network
    git operations (clone / fetch) so that credentials such as ``GH_TOKEN`` are available for
    private-repo access on the host side — before the container is started.
    """
    git = git or GitClones()
    clone = f"{tasks_root.rstrip('/')}/{task_id}"
    env_file = repo.get("env_file")
    env = parse_env(env_file) if env_file else None
    if not exists(clone):
        makedirs(str(Path(clone).parent))
        cache_path = cache.ensure(repo["id"], repo["git_url"], env=env)
        git.clone_local(cache_path=cache_path, dest=clone)
    git.set_origin(repo_path=clone, url=repo["git_url"])
    return clone
