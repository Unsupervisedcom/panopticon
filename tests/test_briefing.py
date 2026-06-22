"""The per-turn state briefing (`core.briefing`): tells the agent which phase it's in.

Rendered from the workflow + task (LLM-free), emitted by the container's user-prompt hook so the
agent doesn't charge ahead — e.g. start implementing during a parity task's PLANNING phase.
"""

from __future__ import annotations

from panopticon.core.briefing import render_state_briefing
from panopticon.workflows import Parity


def test_briefing_names_the_phase_responsibilities_and_user_advance() -> None:
    wf = Parity()
    task = wf.start_task("t1", "r1", at="t0")  # initial state: PLANNING (user-advanced)

    text = render_state_briefing(wf, task)

    assert "PLANNING" in text  # the agent learns which phase it's in
    assert "later phase" in text  # ... and not to do later-phase work (e.g. implementing)
    assert "plan-written" in text and "plan artifact" in text  # this phase's responsibility
    assert "ITERATING" in text  # the advance target
    # PLANNING is user-advanced, so the agent should hand back, not advance itself
    assert "hand back to the user" in text and "Don't advance on your own" in text


def test_briefing_for_an_agent_advanced_phase() -> None:
    wf = Parity()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "MERGING", at="t1")

    text = render_state_briefing(wf, task)

    assert "MERGING" in text and "pr-merged" in text
    assert "advance the task yourself" in text  # MERGING is agent-advanced (background)


def test_briefing_for_a_terminal_state() -> None:
    wf = Parity()
    task = wf.force_transition(wf.start_task("t1", "r1", at="t0"), "COMPLETE", at="t1")

    text = render_state_briefing(wf, task)

    assert "COMPLETE" in text and "finished" in text  # nothing to do in a terminal state
