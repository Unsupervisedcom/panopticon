"""The terminal session supervisor loop (ADR 0009).

The dashboard (`select`) and the tmux attach (`attach`) are injected, so the hub-and-spoke loop
is tested without a TTY or tmux.
"""

from __future__ import annotations

from panopticon.terminal.console import run_console


def test_loop_attaches_each_selected_session_then_stops_on_quit() -> None:
    # The supervisor shows the dashboard, attaches to each picked session, and re-shows the
    # dashboard on detach — until the dashboard returns None (quit).
    picks = iter(["sess-a", "sess-b", None])
    attached: list[str] = []

    run_console(
        client=object(),  # type: ignore[arg-type]  # unused: select/attach are injected
        select=lambda _client: next(picks),
        attach=attached.append,
    )

    assert attached == ["sess-a", "sess-b"]  # one attach per pick, in order; None ends the loop


def test_quitting_immediately_attaches_nothing() -> None:
    attached: list[str] = []
    run_console(
        client=object(),  # type: ignore[arg-type]
        select=lambda _client: None,
        attach=attached.append,
    )
    assert attached == []
