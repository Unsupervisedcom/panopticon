"""Pure domain-model logic: the container-status composition (no I/O, no service)."""

from __future__ import annotations

import pytest

from panopticon.core.models import (
    ContainerStatus,
    LifecyclePhase,
    compose_container_status,
)


def _compose(
    *,
    terminal: bool = False,
    claimed: bool = True,
    registered: bool = False,
    runner_live: bool = True,
    phase: LifecyclePhase | None = None,
) -> str:
    return compose_container_status(
        terminal=terminal,
        claimed=claimed,
        registered=registered,
        runner_live=runner_live,
        phase=phase,
    ).value


def test_terminal_task_has_no_container_status() -> None:
    # A terminal task wins over everything else — even a (stale) live registration.
    assert _compose(terminal=True, registered=True) == "–"
    assert _compose(terminal=True, claimed=False) == "–"


def test_unclaimed_non_terminal_is_queued() -> None:
    assert _compose(claimed=False) == "queued"
    assert _compose(claimed=False, runner_live=False) == "queued"


def test_open_registration_is_live_regardless_of_phase_or_runner() -> None:
    # The container holds its own /live connection, so a registration means live even if the
    # runner's own liveness dropped or a stale spawn phase lingers.
    assert _compose(registered=True) == "live"
    assert _compose(registered=True, runner_live=False) == "live"
    assert _compose(registered=True, phase=LifecyclePhase.AWAITING) == "live"


def test_dead_runner_is_disconnected_even_with_a_stale_phase() -> None:
    assert _compose(runner_live=False) == "disconnected"
    assert _compose(runner_live=False, phase=LifecyclePhase.BUILDING) == "disconnected"


@pytest.mark.parametrize(
    "phase, expected",
    [
        (LifecyclePhase.CLAIMING, "claiming"),
        (LifecyclePhase.PREPARING, "preparing"),
        (LifecyclePhase.BUILDING, "building"),
        (LifecyclePhase.STARTING, "starting"),
        (LifecyclePhase.AWAITING, "awaiting"),
        (LifecyclePhase.FAILED, "failed"),
    ],
)
def test_a_reported_phase_shows_through(phase: LifecyclePhase, expected: str) -> None:
    assert _compose(phase=phase) == expected
    assert ContainerStatus(expected)  # each phase maps to a real status value


def test_claimed_live_runner_no_phase_no_registration_is_down() -> None:
    # Came up and vanished (reconcile cleared the phase), or never reported one.
    assert _compose(phase=None) == "down"
