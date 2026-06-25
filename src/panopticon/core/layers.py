"""The layer-store interface — read Dockerfile *layer files* by name (ADR 0005's repo tier).

A repo's image layer is a **file reference** (a name resolved relative to a configured layers
directory), not inline DB content: the task service reads it to serve over REST, and the runner
composes it onto ``base → workflow → repo``. This module owns the read-only store interface; the
filesystem adapter (and its containment-checked name→path resolution) lives in the task service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from panopticon.core.artifacts import ArtifactError


class InvalidLayerName(ArtifactError):
    """Raised for a layer name that would escape the layers root (e.g. ``../`` or an absolute path)."""


class LayerStore(ABC):
    """Read layer files (Dockerfile fragments) by name, relative to a configured root."""

    @abstractmethod
    def get(self, name: str) -> bytes | None:
        """Return the layer file's bytes, or ``None`` if no such file exists."""
