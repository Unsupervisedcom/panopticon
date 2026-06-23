"""The artifact-store interface + the shared id→path→URI resolver (ADR 0003).

Freeform per-task files (plan, notes) are file-backed, not in the DB. The same bytes are
reachable via the filesystem, the dashboard, and MCP; this module owns the single resolver
that maps ``(task_id, name)`` to a path and an MCP URI so every surface agrees.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

MCP_URI_SCHEME = "panopticon"


class ArtifactError(Exception):
    """Base class for artifact-store failures."""


class InvalidArtifactName(ArtifactError):
    """Raised for an artifact name (or task id) that could escape its directory."""


def validate_segment(segment: str) -> None:
    """Reject names/ids that contain path separators, dot-segments, or are empty."""
    if (
        not segment
        or "/" in segment
        or "\\" in segment
        or segment in (".", "..")
        or segment.startswith(".")
    ):
        raise InvalidArtifactName(f"invalid artifact segment: {segment!r}")


def mcp_uri(task_id: str, name: str) -> str:
    """The canonical MCP resource URI for an artifact (the shared resolver)."""
    validate_segment(task_id)
    validate_segment(name)
    return f"{MCP_URI_SCHEME}://tasks/{task_id}/artifacts/{name}"


class ArtifactStore(ABC):
    """Read/write per-task artifact files."""

    @abstractmethod
    def put(self, task_id: str, name: str, content: bytes) -> None:
        """Create or overwrite an artifact."""

    @abstractmethod
    def get(self, task_id: str, name: str) -> bytes | None:
        """Return artifact bytes, or ``None`` if it does not exist."""

    @abstractmethod
    def list(self, task_id: str) -> list[str]:
        """Return the names of a task's artifacts (empty if none)."""

    def link_slug(self, task_id: str, slug: str) -> None:
        """Expose a task's artifacts under a readable ``slug`` alias (best-effort).

        Symlinks are a filesystem concept, so the default is a no-op; the filesystem adapter
        overrides it. Non-filesystem stores inherit the no-op rather than being forced to model
        an alias they have no notion of.
        """

    def unlink_slug(self, slug: str) -> None:
        """Remove a slug alias created by :meth:`link_slug` (best-effort no-op default)."""
