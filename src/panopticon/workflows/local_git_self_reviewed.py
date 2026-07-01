"""The LocalGitSelfReviewed workflow — a GitHub-free, forge-free lifecycle.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). For repos
where the work stays local — no remote push required, no PR, no CI pipeline, no remote
merge queue. The agent implements and commits locally; the user reviews the diff
themselves and approves the work by advancing `ITERATING → MERGING`; the agent then
merges the task branch into the base branch (via the `local-merge` skill) and advances
itself to `COMPLETE`.

The plan convention (artifact name, shared PLANNING responsibilities, URI resolver, briefing
hook) is inherited from
:class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`. No ``gh`` tool and no
image layer — only the `local-merge` skill and the universal
:func:`~panopticon.core.provisioning` ``provision`` skill that every task receives.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.workflows.planned_workflow import PlannedWorkflow


class LocalGitSelfReviewed(PlannedWorkflow):
    """The local-git-self-reviewed lifecycle: code is committed locally, the **user
    self-reviews** and approves by advancing to MERGING, then the agent merges the
    branch and advances to COMPLETE. No forge dependency."""

    name: ClassVar[str] = "local-git-self-reviewed"
    auto_submit_memo: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Local commits only, no remote push or PR — use when the work stays in the local repo; "
        "you approve the diff and the agent merges the branch."
    )

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
            "The user self-reviews and approves the change by advancing to MERGING."
        )
        responsibilities = (
            Responsibility(key="plan-implemented", description="The plan is implemented in code."),
            Responsibility(key="requests-implemented", description="All user requests are implemented in code."),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(key="committed", description="Changes are committed to the local branch."),
        )
        transitions = ("MERGING",)  # the user self-reviews, then advances to MERGING

    class Merging(State):
        label = "MERGING"
        description = "Merge the task branch into the repo's base branch."
        advanced_by = Actor.AGENT  # background: agent drives the merge and advances itself
        responsibilities = (
            Responsibility(
                key="local-merged",
                description="Changes are merged into the repo's base branch.",
            ),
        )
        transitions = (Complete,)

    initial = Planning

    def skills(self) -> Sequence[Skill]:
        return (
            Skill(
                "local-merge",
                "Merge the task branch into the base branch.",
                "Merge the task branch into the repo's base branch (typically `main`). "
                "Find the current branch with `git -C /workspace branch --show-current`, "
                "then checkout the base branch (`git -C /workspace checkout main`) and "
                "merge with fast-forward disabled (`git -C /workspace merge --no-ff "
                "<task-branch>`). If there are merge conflicts, go back to coding "
                "(`set_state ITERATING`) with an explanation of what conflicted. "
                "Once merged successfully, advance to COMPLETE.",
            ),
        )
