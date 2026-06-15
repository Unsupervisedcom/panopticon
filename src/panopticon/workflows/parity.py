"""The Parity workflow — cloude-cade's lifecycle as a workflow class (ADR 0004, PARITY §1).

`PLANNING → ITERATING → REVIEW → MERGING → COMPLETE` (plus the inherited `DROPPED`), the
prototype's core flow expressed declaratively. The foreground/background split (PARITY §1) is
just `advanced_by`: PLANNING/ITERATING/REVIEW need user approval to leave (`USER`, the
default), while MERGING is agent-driven and advances itself once the change is merged
(`AGENT`). Every non-terminal state seeds the agent's responsibilities for that stage.

`iterate` (return to coding) is the backward edge REVIEW/MERGING → ITERATING; `advance` and
`drop` are the forward edge and the universal `DROPPED` escape. Iterating back is a normal
(gated) transition: taking it means the stage didn't pass, so the agent resolves the unmet
responsibility `FAILED` with a reason (recorded in history) before retreating.

**Responsibilities mirror cloude-cade's per-stage Definition-of-Done** (`bin/cloude_stages.py`'s
`dod_bullets`), with two model divergences (ADR 0004): they are **agent-only**, so cloude-cade's
"The user has approved the plan" is *not* a responsibility — the user approving *is* the advance
out of PLANNING (its plan-accepted hook); and the terminal "the task file has TODO state X"
bullets fall away because DB state replaces org-mode mechanics. cloude-cade's "A draft PR has
been created" is **provisioning** here (ADR 0004's provision seam), not a responsibility. The
forge-tied responsibilities (CI passing, PR updated/reviewed/merged) are the real DoD and gate
now; the *skills* that help fulfil them — `babysit-ci`/`babysit-merge`, PR creation — are
workflow-specific in-container skills that land in a later slice, so `skills()` is empty here.
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
            Responsibility(key="plan-written", description="The plan is written into the plan artifact."),
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        responsibilities = (
            Responsibility(key="plan-implemented", description="The plan is implemented in code."),
            Responsibility(key="requests-implemented", description="All user requests are implemented in code."),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(key="committed-pushed", description="Changes are committed and pushed."),
            Responsibility(key="ci-passing", description="CI tests are passing, or any failures are irrelevant flakes."),
            Responsibility(
                key="pr-updated",
                description="The PR title and description reflect the final change, with no Test Plan / Verification section.",
            ),
        )
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        responsibilities = (
            Responsibility(key="pr-reviewed", description="The PR has been reviewed."),
        )
        transitions = ("MERGING", "ITERATING")  # advance, or iterate back to coding
        operations = {"advance": "MERGING"}  # disambiguate the forward edge (back-edge still present here)

    class Merging(State):
        label = "MERGING"
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (
            Responsibility(key="pr-merged", description="The PR is merged."),
        )
        transitions = (Complete, "ITERATING")  # auto-advance to COMPLETE, or iterate back
        operations = {"advance": "COMPLETE"}

    initial = Planning
