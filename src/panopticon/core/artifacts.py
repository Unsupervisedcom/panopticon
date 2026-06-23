"""The artifact-store interface + the shared id→path→URI resolver (ADR 0003).

Freeform per-task files (plan, notes) are file-backed, not in the DB. The same bytes are
reachable via the filesystem, the dashboard, and MCP; this module owns the single resolver
that maps ``(task_id, name)`` to a path and an MCP URI so every surface agrees.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

MCP_URI_SCHEME = "panopticon"

#: The canonical artifact name for a task's plan. The "plan" is, by convention, a markdown
#: ``plan.md`` artifact (not a working-tree file) — the forge workflows gate PLANNING on it and the
#: orchestrator seeds it for the children it spawns. Single-sourced here, beside the URI resolver,
#: so every surface agrees on the name *and* the URI agents read it back at (:func:`plan_uri`).
PLAN_ARTIFACT_NAME = "plan.md"


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


def plan_uri(task_id: str) -> str:
    """The canonical MCP resource URI for a task's plan artifact (:data:`PLAN_ARTIFACT_NAME`).

    The one URI an agent should read the plan back at — surfaced in the state briefing so
    orchestrator-spawned agents don't guess (e.g. ``artifact://<id>/plan.md`` → "Unknown resource").
    """
    return mcp_uri(task_id, PLAN_ARTIFACT_NAME)


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
