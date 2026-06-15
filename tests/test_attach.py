"""The tmux attach command builder."""

from __future__ import annotations

from panopticon.terminal.attach import attach_command


def test_inside_tmux_switches_the_current_client() -> None:
    assert attach_command("panopticon-t1", socket="panopticon", inside_tmux=True) == [
        "tmux", "-L", "panopticon", "switch-client", "-t", "panopticon-t1"
    ]


def test_outside_tmux_attaches_a_new_client() -> None:
    assert attach_command("panopticon-t1", socket="panopticon", inside_tmux=False) == [
        "tmux", "-L", "panopticon", "attach", "-t", "panopticon-t1"
    ]
