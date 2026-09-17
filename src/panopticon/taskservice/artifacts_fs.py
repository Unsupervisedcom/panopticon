"""Filesystem artifact-store adapter (ADR 0003: local filesystem first).

Layout: ``<root>/tasks/<task_id>/<name>`` for a task's artifacts and
``<root>/repos/<repo_id>/<name…>`` for a repo's (whose names may be nested). The same files are
openable in an editor and, later, served over MCP using the resolver in
:mod:`panopticon.core.artifacts`. Once a task has a slug, ``<root>/tasks/<slug>`` is a relative
symlink to its id-named directory, so a human can reach a task's artifacts by its readable label
as well as its opaque id.
"""

from __future__ import annotations

import asyncio
import builtins
import os
from pathlib import Path

from panopticon.core.artifacts import (
    ArtifactStore,
    InvalidArtifactName,
    is_hidden,
    validate_relative_name,
    validate_segment,
)


class FilesystemArtifactStore(ArtifactStore):
    """Store artifacts as plain files under a root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _task_dir(self, task_id: str) -> Path:
        validate_segment(task_id)
        return self._root / "tasks" / task_id

    def path(self, task_id: str, name: str) -> Path | None:
        """The artifact's on-disk path, or ``None`` when it doesn't exist. For local callers that
        share this store's filesystem and want the real file (e.g. the dashboard's open-in-place),
        so the ``<root>/tasks/<id>/<name>`` layout stays owned here rather than re-derived."""
        validate_segment(name)
        path = self._task_dir(task_id) / name
        return path if path.is_file() else None

    def task_artifact_dir(self, task_id: str) -> Path | None:
        """The task's artifact **directory**, or ``None`` when it doesn't exist here.

        :meth:`path`'s directory twin — the same local-callers-only contract, answering about the
        whole folder rather than one file, for the dashboard's "open the folder" key. ``None``
        covers both reasons there's nothing to open: the caller doesn't share this store's
        filesystem, or the task has no artifacts yet (the directory is created on first write).
        It is the **id**-named directory, not the ``<root>/tasks/<slug>`` alias symlink: that one
        is a convenience for humans browsing by label, while this is the canonical location and
        exists whether or not the task is slugged.
        """
        task_dir = self._task_dir(task_id)
        return task_dir if task_dir.is_dir() else None

    async def put(self, task_id: str, name: str, content: bytes) -> None:
        validate_segment(name)
        task_dir = self._task_dir(task_id)
        await asyncio.to_thread(task_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((task_dir / name).write_bytes, content)

    async def get(self, task_id: str, name: str) -> bytes | None:
        validate_segment(name)
        path = self._task_dir(task_id) / name
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def list(self, task_id: str) -> list[str]:
        task_dir = self._task_dir(task_id)
        if not await asyncio.to_thread(task_dir.is_dir):
            return []
        return await asyncio.to_thread(
            lambda: sorted(p.name for p in task_dir.iterdir() if p.is_file())
        )

    def _has_artifacts_sync(self, task_id: str) -> bool:
        """Whether the task's directory holds at least one unhidden file.

        Synchronous because it is blocking filesystem I/O: every public method here keeps that
        work off the event loop by handing it to a worker thread, and this is the body
        :meth:`has_unhidden_artifacts` hands over. It's a named method rather than the inline
        ``lambda`` the other methods use because it needs ``with`` and ``try``, which a lambda
        can't hold — the same reason :meth:`_link_slug_sync` is split out from its caller.

        ``os.scandir`` rather than ``Path.iterdir``: it stops at the first hit (the caller wants
        a boolean, not a listing), its entries answer ``is_file()`` from the data the scan already
        returned instead of a ``stat`` apiece, and it raises for a missing directory *here* rather
        than lazily on iteration — ``Path.iterdir`` is a generator before 3.13, so the error would
        escape this ``try`` on the Python versions we still support.
        """
        try:
            with os.scandir(self._task_dir(task_id)) as entries:
                return any(not is_hidden(entry.name) and entry.is_file() for entry in entries)
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def has_unhidden_artifacts(self, task_id: str) -> bool:
        """Scan the task's directory directly rather than going through :meth:`list`.

        The inherited default builds and sorts the full name list to answer a question the first
        unhidden entry settles. The task list asks this for every visible task on every refresh,
        so the shortcut is worth the override.
        """
        return await asyncio.to_thread(self._has_artifacts_sync, task_id)

    # -- repo-scoped artifacts ----------------------------------------------------
    #
    # ``<root>/repos/<repo_id>/<name…>`` — the repo sibling of the ``tasks/`` namespace above, so
    # the two scopes can never collide even when a repo and a task share an id. Names here may be
    # nested (``notes/api.md``), which is the one real difference in the implementations: writes
    # create intermediate directories and listing recurses.

    def _repo_dir(self, repo_id: str) -> Path:
        validate_segment(repo_id)
        return self._root / "repos" / repo_id

    def _repo_file(self, repo_id: str, name: str) -> Path:
        """The on-disk path of a repo artifact, guarded against escaping its repo directory.

        :func:`validate_relative_name` already refuses ``..``, an absolute name and the other
        traversal spellings; resolving the join and re-checking the parentage is the belt to that
        braces — a **symlinked** subdirectory planted in the tree points somewhere the name itself
        looks innocent about, and only a resolved path reveals it.
        """
        validate_relative_name(name)
        repo_dir = self._repo_dir(repo_id)
        path = repo_dir / name
        if repo_dir.resolve() not in path.resolve().parents:
            raise InvalidArtifactName(f"artifact name {name!r} escapes the repo directory")
        return path

    def repo_artifact_path(self, repo_id: str, name: str) -> Path | None:
        """A repo artifact's on-disk path, or ``None`` when it doesn't exist — :meth:`path`'s
        repo-scoped twin, for local callers that share this store's filesystem (the dashboard's
        open-in-place)."""
        path = self._repo_file(repo_id, name)
        return path if path.is_file() else None

    def repo_artifact_dir(self, repo_id: str) -> Path | None:
        """The repo's artifact **directory**, or ``None`` when it doesn't exist here.

        The dashboard's "open the folder" key hands this to the host's file manager, so unlike
        :meth:`repo_artifact_path` it answers about a directory rather than one file. ``None``
        covers both reasons there's nothing to open: the dashboard doesn't share this store's
        filesystem, or the repo has no artifacts yet.
        """
        repo_dir = self._repo_dir(repo_id)
        return repo_dir if repo_dir.is_dir() else None

    async def put_repo_artifact(self, repo_id: str, name: str, content: bytes) -> None:
        path = self._repo_file(repo_id, name)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)

    async def get_repo_artifact(self, repo_id: str, name: str) -> bytes | None:
        path = self._repo_file(repo_id, name)
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_bytes)

    def _list_repo_artifacts_sync(self, repo_id: str) -> builtins.list[str]:
        """Every file under the repo's directory as a ``/``-separated relative name, sorted.

        Synchronous because it's blocking I/O the async method hands to a worker thread, like
        :meth:`_has_artifacts_sync`. ``rglob`` recurses (nested names are the point) and
        ``as_posix`` keeps the wire form ``/``-separated on a Windows host, so the name a caller
        reads back is the same name it can pass to :meth:`get_repo_artifact`.
        """
        repo_dir = self._repo_dir(repo_id)
        if not repo_dir.is_dir():
            return []
        return sorted(
            path.relative_to(repo_dir).as_posix() for path in repo_dir.rglob("*") if path.is_file()
        )

    async def list_repo_artifacts(self, repo_id: str) -> builtins.list[str]:
        # ``builtins.list``: :meth:`list` shadows the builtin inside this class body.
        return await asyncio.to_thread(self._list_repo_artifacts_sync, repo_id)

    def _link_slug_sync(self, task_id: str, slug: str) -> None:
        validate_segment(task_id)
        validate_segment(slug)
        link = self._root / "tasks" / slug
        if link.is_symlink():
            if link.readlink() == Path(task_id):
                return  # already the right alias
            link.unlink()
        elif link.exists():
            raise InvalidArtifactName(f"slug {slug!r} collides with an existing artifact entry")
        # Ensure the target exists so the alias resolves immediately rather than dangling.
        self._task_dir(task_id).mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(task_id, target_is_directory=True)

    async def link_slug(self, task_id: str, slug: str) -> None:
        """Alias ``<root>/tasks/<slug>`` to the task's id-named directory.

        The link is **relative** (its target is just ``<task_id>``, a sibling under
        ``tasks/``) so the whole root stays relocatable. Idempotent, and it refuses to clobber
        a real (non-symlink) entry — the slug is validated like any other path segment first.
        """
        await asyncio.to_thread(self._link_slug_sync, task_id, slug)

    async def unlink_slug(self, slug: str) -> None:
        """Remove a slug alias (only if it is a symlink); ignore an absent one."""
        validate_segment(slug)
        link = self._root / "tasks" / slug
        await asyncio.to_thread(lambda: link.unlink() if link.is_symlink() else None)
