"""Filesystem artifact-store adapter (ADR 0003: local filesystem first).

Layout: ``<root>/tasks/<task_id>/<name>``. The same files are openable in an editor and,
later, served over MCP using the resolver in :mod:`panopticon.core.artifacts`.
"""

from __future__ import annotations

from pathlib import Path

from panopticon.core.artifacts import ArtifactStore, validate_segment


class FilesystemArtifactStore(ArtifactStore):
    """Store artifacts as plain files under a root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _task_dir(self, task_id: str) -> Path:
        validate_segment(task_id)
        return self._root / "tasks" / task_id

    def put(self, task_id: str, name: str, content: bytes) -> None:
        validate_segment(name)
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / name).write_bytes(content)

    def get(self, task_id: str, name: str) -> bytes | None:
        validate_segment(name)
        path = self._task_dir(task_id) / name
        return path.read_bytes() if path.is_file() else None

    def list(self, task_id: str) -> list[str]:
        task_dir = self._task_dir(task_id)
        if not task_dir.is_dir():
            return []
        return sorted(p.name for p in task_dir.iterdir() if p.is_file())
