"""Filesystem layer-store adapter: read Dockerfile *layer files* under a root directory.

Layout: ``<root>/<name>`` (nested names allowed; ``..``/absolute escapes rejected). A repo's
``image_layer_file`` is one such name; the task service reads it to serve over REST
(``GET /repos/{id}/image-layer``) and the runner composes it onto ``base → workflow → repo``
(ADR 0005). Read-only — operators populate the root out of band.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from panopticon.core.dirs import LAYERS_DIR
from panopticon.core.layers import InvalidLayerName, LayerStore


class FilesystemLayerStore(LayerStore):
    """Read layer files as plain files under a root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _resolve(self, name: str) -> Path:
        """Resolve ``name`` against the root, refusing anything that escapes it (``..``, absolute)."""
        root = self._root.resolve()
        path = (root / name).resolve()
        if path != root and root not in path.parents:
            raise InvalidLayerName(f"layer name {name!r} escapes the layers root")
        return path

    async def get(self, name: str) -> bytes | None:
        path = self._resolve(name)
        if not await asyncio.to_thread(path.is_file):
            return None
        return await asyncio.to_thread(path.read_bytes)
