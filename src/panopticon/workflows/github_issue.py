"""The GithubIssue workflow — read a linked GitHub issue and land a fix for it.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). The same collapsed
graph as :class:`~panopticon.workflows.github_self_reviewed.GithubSelfReviewed` (no peer
`REVIEW` state — the user self-reviews the fix and approves it by advancing out of `ITERATING`),
specialised for fixing a GitHub issue:

- The **task memo is a link to a GitHub issue** — the thing to fix. Its URL is an **input**,
  read during PLANNING (via the ``read-issue`` skill, ``gh issue view``), not an ITERATING output.
- **PLANNING produces a fix plan grounded in the issue.** The plan (`plan.md`) must satisfy
  :data:`GithubIssue.ISSUE_UNDERSTOOD`: the reported problem, the root cause, how the fix is
  reproduced/confirmed, the fix approach, and the tests that prove it — so "understand the issue
  before coding" is a gated checkbox, not merely implied by a plan existing.
- **The PR is still a fresh output**, opened during ITERATING with the inherited ``open-pr``
  skill; its body closes the issue (``Closes #<n>``, noted by ``read-issue``). Because the PR URL
  is produced in ITERATING (not a known input like Dependabot's), the shared ``url-recorded``
  responsibility stays in ITERATING exactly as in ``github-self-reviewed``.

The forge plumbing (the ``gh`` tool, its image layer, and the ``open-pr``/``babysit-ci``/
``babysit-merge`` skills) is shared with the other forge lifecycles via
:class:`~panopticon.workflows.github_forge.GithubForgeWorkflow`; only the states and the extra
``read-issue`` skill differ.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.workflows.github_forge import GithubForgeWorkflow

#: PLANNING responsibility specific to fixing an issue: the `plan.md` must actually engage with
#: the linked issue, on all five axes. On top of the shared PLAN_WRITTEN (plan is a markdown
#: artifact) and TOKEN_ESTIMATED, so "understand the issue" is a gated checkbox — not merely
#: implied by a plan existing. Defined at module scope so the nested `Planning` state body can
#: reference it (a nested class body can't see the enclosing class's namespace); re-exported as
#: ``GithubIssue.ISSUE_UNDERSTOOD``.
ISSUE_UNDERSTOOD = Responsibility(
    key="issue-understood",
    description=(
        "The plan engages with the linked GitHub issue (the task memo) on all five axes: "
        "(1) the problem the issue reports — expected vs. actual behaviour for a bug, or the "
        "desired behaviour / acceptance criteria for a feature request, (2) the root cause — the "
        "code area/file responsible for a bug, or where the change must land for a feature, "
        "(3) how the fix is reproduced or confirmed — repro steps for a bug, or how it is "
        "verified against the issue's acceptance criteria, (4) the concrete fix approach, and "
        "(5) the tests to add or update that prove the issue is fixed and guard against "
        "regression."
    ),
)


class GithubIssue(GithubForgeWorkflow):
    """The github-issue lifecycle: the task memo is a link to a GitHub issue, which is **read**
    and turned into a fix plan during PLANNING, implemented in ITERATING as a fresh PR that
    closes the issue, **user self-reviewed** and approved by advancing out of ITERATING, then
    shepherded through the merge queue. Foreground states are user-advanced; MERGING is
    agent-driven."""

    name: ClassVar[str] = "github-issue"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "A GitHub issue to fix (task memo = the issue link) — read the issue, plan and implement "
        "a fix, open a PR that closes it, and land it."
    )

    #: Re-export of the module-level issue-comprehension responsibility (see
    #: :data:`ISSUE_UNDERSTOOD`).
    ISSUE_UNDERSTOOD: ClassVar[Responsibility] = ISSUE_UNDERSTOOD

    class Planning(InitialState):
        label = "PLANNING"
        description = (
            "Name the task (`/provision`), then run `read-issue` to read the linked GitHub issue "
            "(the task memo) — its title, body, labels, and discussion — and note its number so "
            "the PR you open later closes it. Produce a plan (`plan.md`) that engages with the "
            "issue on five axes: the problem it reports (expected vs. actual for a bug, or the "
            "desired behaviour / acceptance criteria for a feature), the root cause in the code, "
            "how the fix will be reproduced or confirmed, the concrete fix approach, and the "
            "tests that prove it. The issue URL is an input; the PR you open in ITERATING is the "
            "recorded task URL."
        )
        responsibilities = (  # shared plan/token promises + the issue-specific comprehension
            GithubForgeWorkflow.PLAN_WRITTEN,
            GithubForgeWorkflow.TOKEN_ESTIMATED,
            ISSUE_UNDERSTOOD,
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Implement the fix per the plan. Open a PR (`open-pr`) whose body closes the linked "
            "issue (`Closes #<n>`, the number noted from `read-issue`). Implement any additional "
            "user requests or feedback. Keep tests green and push. The user self-reviews the fix "
            "and approves by advancing to MERGING."
        )
        responsibilities = (
            Responsibility(
                key="plan-implemented", description="The fix from the plan is implemented in code."
            ),
            Responsibility(
                key="requests-implemented", description="All user requests are implemented in code."
            ),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(key="committed-pushed", description="Changes are committed and pushed."),
            Responsibility(
                key="ci-passing",
                description="CI tests are passing, or any failures are irrelevant flakes.",
            ),
            Responsibility(
                key="pr-updated",
                description="The PR title and description reflect the final change, with no Test Plan / Verification section.",
            ),
            GithubForgeWorkflow.URL_RECORDED,  # the PR is opened here, so its URL is recorded here
        )
        transitions = ("MERGING",)  # no REVIEW: the user self-reviews, then advances to MERGING

    class Merging(State):
        label = "MERGING"
        description = "Add the PR to the merge queue. If the PR exits the merge queue, re-add it."
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (Responsibility(key="pr-merged", description="The PR is merged."),)
        transitions = (Complete,)  # the happy path; `advance` derives → COMPLETE

    initial = Planning

    def skills(self) -> Sequence[Skill]:
        """Add a ``read-issue`` skill (read the linked issue, note its number) on top of the
        inherited forge skills (``open-pr`` / ``babysit-ci`` / ``babysit-merge``), reused
        verbatim — the PR is still opened normally, it just closes the issue."""
        return (
            Skill(
                "read-issue",
                "Read the linked GitHub issue (the task memo) and note its number for the PR.",
                "The task memo is a link to the GitHub issue to fix — read it during PLANNING so "
                "the plan is grounded in the actual report.\n"
                "1. Make sure the task is provisioned first (`/provision` — set the slug) so "
                "`origin` points at the forge; `gh` needs the forge remote.\n"
                "2. Read the issue URL from the task memo.\n"
                "3. Read the issue with `gh issue view <url> --comments` — its title, body, "
                "labels, and discussion — and extract the acceptance criteria (what 'fixed' "
                "means).\n"
                "4. Note the issue **number**: when you open the PR in ITERATING (`open-pr`), put "
                "`Closes #<n>` in its body so merging the PR closes the issue.",
            ),
            *super().skills(),
        )
