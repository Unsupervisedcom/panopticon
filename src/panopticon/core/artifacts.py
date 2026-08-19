"""The artifact-store interface + the shared id→path→URI resolver (ADR 0003).

Freeform per-task files (plan, notes) are file-backed, not in the DB. The same bytes are
reachable via the filesystem, the dashboard, and MCP; this module owns the single resolver
that maps ``(task_id, name)`` to a path and an MCP URI so every surface agrees.
"""

from __future__ import annotations

import binascii
from abc import ABC, abstractmethod
from base64 import b64decode
from urllib.parse import quote, unquote

MCP_URI_SCHEME = "panopticon"


class ArtifactError(Exception):
    """Base class for artifact-store failures."""


class InvalidArtifactName(ArtifactError):
    """Raised for an artifact name (or task id) that could escape its directory."""


class InvalidArtifactContent(ArtifactError):
    """Raised for artifact content that can't be decoded (e.g. malformed base64)."""


def decode_b64_artifact(name: str, encoded: str) -> bytes:
    """Decode a base64-encoded artifact value to raw bytes, raising :class:`ArtifactError` on
    malformed input. This is how binary artifacts (screenshots, PDFs) cross the JSON surfaces
    (REST create body, MCP tools) — JSON can't carry raw bytes, so the value is base64."""
    try:
        return b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidArtifactContent(f"artifact {name!r}: invalid base64: {exc}") from exc


def validate_segment(segment: str) -> None:
    """Reject names/ids that contain path separators, are empty, or are the dot-sentinels ``.`` / ``..``.

    Dotfile names (e.g. ``.babysit-ci-state.json``) are valid — only the bare directory
    sentinels are forbidden."""
    if not segment or "/" in segment or "\\" in segment or segment in (".", ".."):
        raise InvalidArtifactName(f"invalid artifact segment: {segment!r}")


def mcp_uri(task_id: str, name: str) -> str:
    """The canonical MCP resource URI for an artifact (the shared resolver).

    The ``task_id`` and ``name`` are **percent-encoded** into the path so a name with spaces or
    other URI-reserved characters (``my notes.md``, ``a+b.md``) yields a valid, unambiguous URI.
    :func:`decode_segment` reverses this in the resource handler — the two must stay paired."""
    validate_segment(task_id)
    validate_segment(name)
    return f"{MCP_URI_SCHEME}://tasks/{quote(task_id, safe='')}/artifacts/{quote(name, safe='')}"


def decode_segment(segment: str) -> str:
    """Percent-decode a path segment extracted from an MCP artifact URI, reversing the encoding
    :func:`mcp_uri` applied. The MCP resource layer matches the URI template but does **not**
    decode the captured segments, so the handler must (e.g. ``my%20notes.md`` → ``my notes.md``)."""
    return unquote(segment)


class ArtifactStore(ABC):
    """Read/write per-task artifact files."""

    @abstractmethod
    async def put(self, task_id: str, name: str, content: bytes) -> None:
        """Create or overwrite an artifact."""

    @abstractmethod
    async def get(self, task_id: str, name: str) -> bytes | None:
        """Return artifact bytes, or ``None`` if it does not exist."""

    @abstractmethod
    async def list(self, task_id: str) -> list[str]:
        """Return the names of a task's artifacts (empty if none)."""

    async def link_slug(self, task_id: str, slug: str) -> None:
        """Expose a task's artifacts under a readable ``slug`` alias (best-effort).

        Symlinks are a filesystem concept, so the default is a no-op; the filesystem adapter
        overrides it. Non-filesystem stores inherit the no-op rather than being forced to model
        an alias they have no notion of.
        """

    async def unlink_slug(self, slug: str) -> None:
        """Remove a slug alias created by :meth:`link_slug` (best-effort no-op default)."""
