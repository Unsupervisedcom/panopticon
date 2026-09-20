"""Suite-wide fixtures.

Feature flags (:mod:`panopticon.core.features`) are read from the **process env**, so the suite pins
them rather than inheriting whatever the developer exported: ``_codex_flag_off`` clears
``PANOPTICON_ENABLE_CODEX`` for every test (matching the shipped default, so the "codex is off"
assertions can't pass vacuously on one machine and fail on another), and the ``enable_codex``
fixture turns it on for the tests that exercise codex itself.
"""

from __future__ import annotations

import pytest

from panopticon.core.features import CODEX_FLAG


@pytest.fixture(autouse=True)
def _codex_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with codex disabled — the shipped default (ADR 0014 §7)."""
    monkeypatch.delenv(CODEX_FLAG, raising=False)


@pytest.fixture
def enable_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the codex feature flag on for a test that exercises codex support."""
    monkeypatch.setenv(CODEX_FLAG, "1")
