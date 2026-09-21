"""The pure ask-message composition (ask-the-author): marker + framing, terminal vs in-flight."""

from __future__ import annotations

from panopticon.core.asking import ASK_MARKER_PREFIX, ask_marker, compose_ask_message


def test_ask_marker_embeds_the_id() -> None:
    marker = ask_marker("abc123")
    assert marker == f"[[{ASK_MARKER_PREFIX}:abc123]]"
    # Distinct ids give distinct markers so the Stop hook anchors the right reply.
    assert ask_marker("abc123") != ask_marker("def456")


def test_compose_includes_marker_question_and_context() -> None:
    msg = compose_ask_message("  why a dict?  ", "  see models.py  ", terminal=False, ask_id="x1")
    assert msg.startswith(ask_marker("x1"))  # marker first, so the hook can find the reply
    assert "why a dict?" in msg and "see models.py" in msg
    # Whitespace around the question/context is trimmed.
    assert "  why a dict?  " not in msg


def test_compose_omits_context_when_blank() -> None:
    msg = compose_ask_message("why?", "   ", terminal=False, ask_id="x1")
    assert "Context from the reviewer" not in msg


def test_terminal_message_carries_the_readonly_guardrail() -> None:
    msg = compose_ask_message("why?", "", terminal=True, ask_id="x1")
    assert "merged or proposed work" in msg
    assert "not a request to change anything" in msg
    assert "Do not modify files" in msg


def test_non_terminal_message_omits_the_hard_readonly_ban() -> None:
    msg = compose_ask_message("why?", "", terminal=False, ask_id="x1")
    assert "Do not modify files" not in msg
    assert "asking you a question" in msg
