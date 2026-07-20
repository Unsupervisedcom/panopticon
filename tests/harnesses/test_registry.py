"""The harness registry: name → adapter lookup, the claude default, unknown-name rejection.

Chunk 1 registers claude only; codex arrives in a later chunk and extends these expectations."""

from __future__ import annotations

import pytest

from panopticon.harnesses import DEFAULT_HARNESS, HARNESSES, get_harness
from panopticon.harnesses.claude import ClaudeHarness


def test_registry_holds_claude() -> None:
    assert set(HARNESSES) == {"claude"}
    assert isinstance(HARNESSES["claude"], ClaudeHarness)


def test_get_harness_defaults_to_claude() -> None:
    # None = a task recorded before harnesses existed (or one that never chose): the default
    # surface panopticon launched with.
    assert get_harness(None) is HARNESSES[DEFAULT_HARNESS]
    assert get_harness(None).name == "claude"


def test_get_harness_by_name() -> None:
    assert get_harness("claude").name == "claude"


def test_get_harness_rejects_an_unknown_name_listing_the_known_ones() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_harness("cursor")
    assert "cursor" in str(excinfo.value) and "claude" in str(excinfo.value)


def test_config_dirnames_are_distinct_dotdirs() -> None:
    # Each harness gets its own config-volume mountpoint under the container home; a collision
    # would make two harnesses share (and clobber) per-task state.
    dirnames = [h.config_dirname for h in HARNESSES.values()]
    assert len(set(dirnames)) == len(dirnames)
    assert all(d.startswith(".") and "/" not in d for d in dirnames)
