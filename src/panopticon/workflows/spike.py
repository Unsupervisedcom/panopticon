"""The Spike workflow — open-ended, ungated agent work (the free-form workflow).

The companion to the lifecycle-heavy GithubPeerReviewed workflow: a single agent-driven state that
runs until the user marks the task ``COMPLETE`` (or it's ``DROPPED`` via the inherited transition).
No responsibilities, no gates, no forge skills — just the agent working until the user is satisfied.
Standing alongside it proves the Milestone 1 thesis: the lifecycle is the workflow's, not the
engine's — there is no hardcoded lifecycle (GOALS.md, ADR 0004).
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


class Spike(Workflow):
    """ITERATING → {COMPLETE, DROPPED}. Agent-driven, ungated.

    The single state is the workflow's ``initial``, so it's an :class:`InitialState` —
    ``turn_on_enter = USER`` (the agent waits for the user's first instruction), ``advanced_by
    = USER`` (the user marks it complete when satisfied); no further overrides needed.
    """

    name: ClassVar[str] = "spike"

    class Iterating(InitialState):
        label = "ITERATING"
        description = "Open-ended agent work until the user marks the task complete."
        transitions = (Complete,)  # + DROPPED inherited from State

    initial = Iterating
