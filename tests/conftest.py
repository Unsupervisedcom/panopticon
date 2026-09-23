"""Suite-wide fixtures.

Feature flags (:mod:`panopticon.core.features`) are read from the **process env**, so the suite pins
them rather than inheriting whatever the developer exported: ``_codex_flag_off`` clears
``PANOPTICON_ENABLE_CODEX`` for every test (matching the shipped default, so the "codex is off"
assertions can't pass vacuously on one machine and fail on another), and the ``enable_codex``
fixture turns it on for the tests that exercise codex itself.

The container-priority knobs (:mod:`panopticon.sessionservice.priority`) are pinned the same way
and for the same reason: an operator who exported ``PANOPTICON_CONTAINER_CPU_SHARES`` on their box
must not change what the runner's argv assertions see.
"""

from __future__ import annotations

import pytest

from panopticon.core.features import CODEX_FLAG
from panopticon.sessionservice.priority import (
    BLKIO_WEIGHT_VAR,
    CGROUP_PARENT_VAR,
    CPU_SHARES_VAR,
    HOST_NICE_VAR,
    OOM_SCORE_ADJ_VAR,
)


@pytest.fixture(autouse=True)
def _codex_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with codex disabled — the shipped default (ADR 0014 §7)."""
    monkeypatch.delenv(CODEX_FLAG, raising=False)


@pytest.fixture
def enable_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the codex feature flag on for a test that exercises codex support."""
    monkeypatch.setenv(CODEX_FLAG, "1")


@pytest.fixture(autouse=True)
def _priority_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test sees the shipped resource-priority defaults, not the developer's exports."""
    for var in (
        CPU_SHARES_VAR,
        BLKIO_WEIGHT_VAR,
        OOM_SCORE_ADJ_VAR,
        CGROUP_PARENT_VAR,
        HOST_NICE_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
