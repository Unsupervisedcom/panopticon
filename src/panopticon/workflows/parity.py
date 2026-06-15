"""The Parity workflow — cloude-cade's lifecycle as a workflow class (ADR 0004, PARITY §1).

`PLANNING → ITERATING → REVIEW → MERGING → COMPLETE` (plus the inherited `DROPPED`), the
prototype's core flow expressed declaratively. The foreground/background split (PARITY §1) is
just `advanced_by`: PLANNING/ITERATING/REVIEW need user approval to leave (`USER`, the
default), while MERGING is agent-driven and advances itself once the change is merged
(`AGENT`). Every non-terminal state seeds the agent's responsibilities for that stage.

`iterate` (return to coding) is the backward edge REVIEW/MERGING → ITERATING; `advance` and
`drop` are the forward edge and the universal `DROPPED` escape. Iterating back is a normal
(gated) transition: taking it means the stage didn't pass, so the agent resolves the unmet
responsibility `FAILED` with a reason (recorded in history) before retreating. Remote forge behavior — PR
creation, `babysit-ci`/`babysit-merge` — is workflow-specific *in-container skills* and lands
in a later slice (ADR 0004); this class is forge-less, so `skills()` is empty here.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.models import Actor, Responsibility
from panopticon.core.state import Complete, State
from panopticon.core.workflow import Workflow


class Parity(Workflow):
    """The parity lifecycle. Foreground states are user-advanced; MERGING is agent-driven."""

    name: ClassVar[str] = "parity"

    class Planning(State):
        label = "PLANNING"
        responsibilities = (
            Responsibility(key="plan-drafted", description="Draft a plan and capture it in the plan artifact"),
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        responsibilities = (
            Responsibility(key="changes-implemented", description="Implement the planned changes"),
            Responsibility(key="tests-pass", description="The project's tests pass locally"),
        )
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        responsibilities = (
            Responsibility(key="self-reviewed", description="Self-review the diff for correctness, scope, and style"),
        )
        transitions = ("MERGING", "ITERATING")  # advance, or iterate back to coding

    class Merging(State):
        label = "MERGING"
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (
            Responsibility(key="merged", description="The change is merged into the base branch"),
        )
        transitions = (Complete, "ITERATING")  # auto-advance to COMPLETE, or iterate back

    initial = Planning
