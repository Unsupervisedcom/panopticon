"""The GithubSelfReviewed workflow — `github-peer-reviewed` without the peer-review gate.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). Identical to
:class:`~panopticon.workflows.github_peer_reviewed.GithubPeerReviewed` except there is **no
distinct REVIEW state**: with self-review there is no peer to gate the merge. The user reviews
the change themselves during/after ITERATING and approves it by advancing `ITERATING → MERGING`
— that advance is user-gated (`advanced_by = USER`), so "tell the agent to proceed to merging"
*is* the approval. The peer-only `pr-reviewed` responsibility falls away with the REVIEW state.

The forge plumbing (the `gh` tool, its image layer, and the `open-pr`/`babysit-ci`/`babysit-merge`
skills) is shared with the peer-reviewed lifecycle via
:class:`~panopticon.workflows.github_forge.GithubForgeWorkflow`; only the states differ.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.models import Actor, Responsibility
from panopticon.core.state import Complete, State
from panopticon.workflows.github_forge import PLAN_WRITTEN, GithubForgeWorkflow


class GithubSelfReviewed(GithubForgeWorkflow):
    """The github-self-reviewed lifecycle: code reaches GitHub and the **user self-reviews**,
    approving the merge by advancing out of ITERATING. Foreground states are user-advanced;
    MERGING is agent-driven."""

    name: ClassVar[str] = "github-self-reviewed"

    class Planning(State):
        label = "PLANNING"
        description = "Collect requirements. Produce a plan for the implementation."
        responsibilities = (PLAN_WRITTEN,)  # shared: the plan is a markdown `plan.md` artifact
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Implement the plan. Implement any additional user requests or feedback. Implement "
            "any review comments the user has approved for implementation. The user self-reviews "
            "and approves the change by advancing to MERGING."
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
        transitions = ("MERGING",)  # no REVIEW: the user self-reviews, then advances to MERGING

    class Merging(State):
        label = "MERGING"
        description = "Add the PR to the merge queue. If the PR exits the merge queue, re-add it."
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (
            Responsibility(key="pr-merged", description="The PR is merged."),
        )
        transitions = (Complete,)  # the happy path; `advance` derives → COMPLETE

    initial = Planning
