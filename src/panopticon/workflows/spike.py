"""The Spike workflow — the minimal seed workflow.

A *spike* is exploratory work: a single agent-driven state that runs until the task is
marked ``COMPLETE`` (or, via the inherited transition, ``DROPPED``). Its existence proves
the state machine has no hardcoded lifecycle (GOALS.md, ADR 0004): no responsibilities, no gates.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.state import Complete, State
from panopticon.core.workflow import Workflow


class Spike(Workflow):
    """ITERATING → {COMPLETE, DROPPED}. Agent-driven, ungated.

    Takes the ``State`` defaults — ``turn_on_enter = AGENT``, ``advanced_by = USER`` — so the
    agent iterates and the user marks it complete when satisfied; no overrides needed.
    """

    name: ClassVar[str] = "spike"

    class Iterating(State):
        label = "ITERATING"
        transitions = (Complete,)  # + DROPPED inherited from State

    initial = Iterating
