"""Agent-CLI harnesses (Milestone 3): the registry of pluggable in-container agent runtimes.

A task records which harness runs it (:attr:`~panopticon.core.models.Task.harness`, an opaque
string); the container's agent launcher and the session service look the mechanics up here.
The registry is a literal mapping — path discovery (the ``workflows`` treatment) waits until
operator-authored harnesses are a real use, not a hypothetical."""

from __future__ import annotations

from collections.abc import Mapping

from panopticon.harnesses.base import (
    CREDENTIALS_MOUNT,
    INTERRUPT_PROMPT,
    BootstrapContext,
    Harness,
    LaunchContext,
)
from panopticon.harnesses.claude import ClaudeHarness
from panopticon.harnesses.codex import CodexHarness

#: The default when a task records no harness — the surface panopticon launched with.
DEFAULT_HARNESS = "claude"

HARNESSES: Mapping[str, Harness] = {h.name: h for h in (ClaudeHarness(), CodexHarness())}


def get_harness(name: str | None) -> Harness:
    """The harness registered as ``name`` (``None`` → the default). Raises ``KeyError`` with the
    known names for an unknown one — task creation validates against this, so a spawn never
    discovers an unknown harness."""
    key = name or DEFAULT_HARNESS
    try:
        return HARNESSES[key]
    except KeyError:
        raise KeyError(f"unknown harness {key!r} (known: {', '.join(sorted(HARNESSES))})") from None


__all__ = [
    "CREDENTIALS_MOUNT",
    "DEFAULT_HARNESS",
    "HARNESSES",
    "INTERRUPT_PROMPT",
    "BootstrapContext",
    "Harness",
    "LaunchContext",
    "get_harness",
]
