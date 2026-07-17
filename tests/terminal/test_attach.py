"""The tmux attach command builder."""

from __future__ import annotations

from panopticon.terminal.attach import attach_command


def test_attaches_the_terminal_to_the_session() -> None:
    assert attach_command("panopticon-t1", socket="panopticon") == [
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        "panopticon-t1",
    ]


def test_remote_host_wraps_the_attach_in_ssh() -> None:
    # M5 shape: the same supervisor loop reaches a remote session by prefixing ssh -t <host>.
    assert attach_command("panopticon-t1", socket="panopticon", host="box") == [
        "ssh",
        "-t",
        "box",
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        "panopticon-t1",
    ]
