"""Filesystem artifact-store adapter (ADR 0003: local filesystem first).

Layout: ``<root>/tasks/<task_id>/<name>``. The same files are openable in an editor and,
later, served over MCP using the resolver in :mod:`panopticon.core.artifacts`. Once a task has
a slug, ``<root>/tasks/<slug>`` is a relative symlink to its id-named directory, so a human can
reach a task's artifacts by its readable label as well as its opaque id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from panopticon.core.artifacts import ArtifactStore, InvalidArtifactName, validate_segment
from panopticon.core.dirs import user_data_dir

#: Default artifact-store root. Shared so the task service and any co-located reader (e.g. the
#: dashboard's open-in-place) resolve the same location from one source rather than copied literals.
DEFAULT_ARTIFACTS: str = str(user_data_dir() / "artifacts")


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
