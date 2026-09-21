"""Pure domain-model logic: the container-status composition (no I/O, no service)."""

from __future__ import annotations

import pytest

from panopticon.core.models import (
    Actor,
    ContainerStatus,
    LifecyclePhase,
    Repo,
    Task,
    compose_container_status,
    resolve_agent_cli,
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
        (LifecyclePhase.HEALING, "healing"),
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


# -- agent-CLI selection (ADR 0014 §3) --------------------------------------------------


def test_repo_defaults_to_claude_and_task_override_is_null() -> None:
    repo = Repo(id="r1", name="r", git_url="https://x/r.git")
    assert repo.agent_cli == "claude"
    task = Task(id="t1", repo_id="r1", workflow="spike", state="PLANNING", turn=Actor.AGENT)
    assert task.agent_cli is None  # None = "use the repo default"


@pytest.mark.parametrize(
    ("task_cli", "repo_cli", "expected"),
    [
        (None, "claude", "claude"),  # no override → repo default
        (None, "codex", "codex"),  # repo default flows through
        ("codex", "claude", "codex"),  # task override wins over the repo default
        ("claude", "codex", "claude"),  # override wins even when it re-selects the base default
        (None, None, "claude"),  # neither set → the "claude" fallback
    ],
)
def test_resolve_agent_cli_order(task_cli: str | None, repo_cli: str | None, expected: str) -> None:
    assert resolve_agent_cli(task_cli, repo_cli) == expected
