"""The LocalGitSelfReviewed workflow — a GitHub-free, forge-free lifecycle.

`PLANNING → ITERATING → COMPLETE` (plus the inherited `DROPPED`). For repos where the
work stays local — no remote push required, no PR, no CI pipeline, no merge queue. The
agent implements and commits locally; the user reviews the diff themselves and approves
the work by advancing `ITERATING → COMPLETE`. That advance is user-gated
(``advanced_by = USER``), so telling the agent to proceed *is* the approval.

The plan convention (artifact name, shared PLANNING responsibilities, URI resolver, briefing
hook) is inherited from
:class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`. No ``gh`` tool, no image
layer, and no forge skills — only the universal :func:`~panopticon.core.provisioning`
``provision`` skill that every task receives.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.models import Responsibility
from panopticon.core.state import Complete, InitialState, State
from panopticon.workflows.planned_workflow import PlannedWorkflow


class LocalGitSelfReviewed(PlannedWorkflow):
    """The local-git-self-reviewed lifecycle: code is committed locally and the **user
    self-reviews**, approving by advancing out of ITERATING. No forge dependency."""

    name: ClassVar[str] = "local-git-self-reviewed"

    class Planning(InitialState):
        label = "PLANNING"
        description = "Collect requirements. Produce a plan for the implementation."
        responsibilities = (
            PlannedWorkflow.PLAN_WRITTEN,
            PlannedWorkflow.TOKEN_ESTIMATED,
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Implement the plan. Implement any additional user requests or feedback. "
            "The user self-reviews and approves the change by advancing to COMPLETE."
        )
        responsibilities = (
            Responsibility(key="plan-implemented", description="The plan is implemented in code."),
            Responsibility(key="requests-implemented", description="All user requests are implemented in code."),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(key="committed", description="Changes are committed to the local branch."),
        )
        transitions = (Complete,)  # the user self-reviews, then advances straight to COMPLETE

    initial = Planning
