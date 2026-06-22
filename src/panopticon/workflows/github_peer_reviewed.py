"""The GithubPeerReviewed workflow — cloude-cade's lifecycle as a workflow class (ADR 0004, PARITY §1).

`PLANNING → ITERATING → REVIEW → MERGING → COMPLETE` (plus the inherited `DROPPED`), the
prototype's core flow expressed declaratively. The foreground/background split (PARITY §1) is
just `advanced_by`: PLANNING/ITERATING/REVIEW need user approval to leave (`USER`, the
default), while MERGING is agent-driven and advances itself once the change is merged
(`AGENT`). Every non-terminal state seeds the agent's responsibilities for that stage.

`advance` is the happy path — the single forward edge of each state, auto-derived — and `drop`
is the universal `DROPPED` escape; those are the only core operations. Going back to coding from
REVIEW/MERGING is **not** a declared transition: it's a **free move** to ITERATING (`set_state`,
ungated), exercised through an agent skill, so a backward edge needn't be in the graph. Only
`advance` along the declared graph is gated by responsibilities.

**Responsibilities mirror cloude-cade's per-stage Definition-of-Done** (`bin/cloude_stages.py`'s
`dod_bullets`), with two model divergences (ADR 0004): they are **agent-only**, so cloude-cade's
"The user has approved the plan" is *not* a responsibility — the user approving *is* the advance
out of PLANNING (the claude plan-accepted hook lands in Slice 6); and the terminal "the task file has TODO state X"
bullets fall away because DB state replaces org-mode mechanics. cloude-cade's "A draft PR has
been created" is **provisioning** here (ADR 0004's provision seam), not a responsibility. The
forge-tied responsibilities (CI passing, PR updated/reviewed/merged) are the real DoD and gate
now; the *skills* that help fulfil them are the forge skills (`open-pr`, `babysit-ci`,
`babysit-merge`) inherited from :class:`~panopticon.workflows.github_forge.GithubForgeWorkflow`,
agent-driven in-container procedures (ADR 0004) the agent runs against `gh`/CI.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.models import Actor, Responsibility
from panopticon.core.state import Complete, State
from panopticon.workflows.github_forge import PLAN_WRITTEN, GithubForgeWorkflow


class GithubPeerReviewed(GithubForgeWorkflow):
    """The github-peer-reviewed lifecycle (formerly ``parity``): code reaches GitHub and a peer
    gates the merge. Foreground states are user-advanced; MERGING is agent-driven."""

    name: ClassVar[str] = "github-peer-reviewed"

    class Planning(State):
        label = "PLANNING"
        description = "Collect requirements. Produce a plan for the implementation."
        responsibilities = (PLAN_WRITTEN,)  # shared: the plan is a markdown `plan.md` artifact
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Implement the plan. Implement any additional user requests or feedback. Implement "
            "any review comments the user has approved for implementation."
        )
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
        description = "Wait for review or approval of the PR."
        responsibilities = (
            Responsibility(key="pr-reviewed", description="The PR has been reviewed."),
        )
        transitions = ("MERGING",)  # the happy path; `advance` derives from it

    class Merging(State):
        label = "MERGING"
        description = "Add the PR to the merge queue. If the PR exits the merge queue, re-add it."
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (
            Responsibility(key="pr-merged", description="The PR is merged."),
        )
        transitions = (Complete,)  # the happy path; `advance` derives → COMPLETE

    initial = Planning
