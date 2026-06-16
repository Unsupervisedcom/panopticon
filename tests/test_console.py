"""The terminal session supervisor (ADR 0009 §6).

The dashboard step (`show_dashboard`) and the tmux attach (`attach`) are injected, so the
hub-and-spoke loop is tested without a TTY or tmux; `switch_to`'s detach is injected too.
"""

from __future__ import annotations

from pathlib import Path

from panopticon.terminal.console import run_console, switch_to


def test_loop_attaches_each_picked_session_then_stops_on_quit() -> None:
    # The supervisor shows the dashboard, attaches to each picked session, and re-shows the same
    # dashboard on detach — until the dashboard returns None (quit).
    picks = iter(["sess-a", "sess-b", None])
    attached: list[str] = []

    run_console(show_dashboard=lambda: next(picks), attach=attached.append)

    assert attached == ["sess-a", "sess-b"]  # one attach per pick, in order; None ends the loop


def test_quitting_immediately_attaches_nothing() -> None:
    attached: list[str] = []
    run_console(show_dashboard=lambda: None, attach=attached.append)
    assert attached == []


def test_switch_to_records_the_pick_then_detaches(tmp_path: Path) -> None:
    # The dashboard's `t` hook: write the pick for the supervisor, then detach this client so the
    # supervisor regains the TTY and attaches the task. The dashboard process stays alive.
    detached: list[bool] = []
    switch = tmp_path / "switch"

    switch_to("panopticon-t1", switch_file=switch, detach=lambda: detached.append(True))

    assert switch.read_text() == "panopticon-t1"
    assert detached == [True]
