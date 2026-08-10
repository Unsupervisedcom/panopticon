"""The GithubDependabot workflow — evaluate and land a Dependabot dependency-bump PR.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). The same collapsed
graph as :class:`~panopticon.workflows.github_self_reviewed.GithubSelfReviewed` (no peer
`REVIEW` state — the user self-reviews the evaluation and approves the bump by advancing out of
`ITERATING`), specialised for Dependabot:

- The **PR already exists** — Dependabot opened it, and the task's **memo is a link to that PR**.
  There is nothing to *open*; instead of the inherited ``open-pr`` skill, this workflow provides
  a ``checkout-dependabot-pr`` skill that puts the working tree on the PR's head branch
  (``gh pr checkout``) and records its URL. Any recommended supporting changes are then committed
  and pushed onto that same branch, updating the Dependabot PR. Provisioning is unchanged (the
  ``panopticon/<slug>`` branch is simply unused), so ``core``/``taskservice``/``sessionservice``
  need no changes.
- **PLANNING produces an evaluation**, not an implementation plan. The plan (`plan.md`) must
  evaluate the five points captured by :data:`GithubDependabot.UPGRADE_EVALUATED`: how the
  upgraded module is used, how relevant the change is to this repo, how risky it is (semver scope
  + breaking changes), whether it addresses a security advisory (and how urgent), and any
  recommended supporting changes — *none is an acceptable answer*.

The forge plumbing (the ``gh`` tool, its image layer, and the ``babysit-ci``/``babysit-merge``
skills) is shared with the other forge lifecycles via
:class:`~panopticon.workflows.github_forge.GithubForgeWorkflow`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility, Skill
from panopticon.core.state import Complete, InitialState, State
from panopticon.workflows.github_forge import GithubForgeWorkflow

#: PLANNING responsibility specific to the dependency-bump evaluation: the `plan.md` must cover
#: all five axes. On top of the shared PLAN_WRITTEN (plan is a markdown artifact) and
#: TOKEN_ESTIMATED, so the evaluation is a gated checkbox — not merely implied by a plan existing.
#: Defined at module scope so the nested `Planning` state body can reference it (a nested class
#: body can't see the enclosing class's namespace); re-exported as ``GithubDependabot.UPGRADE_EVALUATED``.
UPGRADE_EVALUATED = Responsibility(
    key="upgrade-evaluated",
    description=(
        "The plan evaluates the Dependabot bump on all five axes: (1) how the upgraded module "
        "is used in this repo, (2) how relevant the change is to that usage, (3) how risky the "
        "upgrade is — including semver scope (patch/minor/major) and any breaking changes, "
        "(4) whether it addresses a known security advisory / CVE and how urgent merging is, and "
        "(5) any recommended changes to support the upgrade (concluding that none are needed is "
        "acceptable)."
    ),
)


class GithubDependabot(GithubForgeWorkflow):
    """The github-dependabot lifecycle: a Dependabot dependency-bump PR (the task memo is the
    PR link) is **evaluated** during PLANNING, the **user self-reviews** the evaluation and
    approves by advancing out of ITERATING, then the agent shepherds the merge. Foreground
    states are user-advanced; MERGING is agent-driven."""

    name: ClassVar[str] = "github-dependabot"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "A Dependabot dependency-bump PR (task memo = the PR link) — evaluate how the bumped "
        "module is used, how relevant/risky the change is, whether it closes a security advisory, "
        "and land it with any supporting changes."
    )

    #: Re-export of the module-level evaluation responsibility (see :data:`UPGRADE_EVALUATED`).
    UPGRADE_EVALUATED: ClassVar[Responsibility] = UPGRADE_EVALUATED

    class Planning(InitialState):
        label = "PLANNING"
        description = (
            "Read the linked Dependabot PR from the task memo (its diff, changelog, and release "
            "notes). Produce a plan (`plan.md`) that evaluates the bump on five axes: how the "
            "upgraded module is used in this repo, how relevant the change is to that usage, how "
            "risky it is (semver scope + breaking changes), whether it addresses a security "
            "advisory / CVE and how urgent it is, and any recommended supporting changes "
            "(concluding that none are needed is acceptable)."
        )
        responsibilities = (  # shared plan/token promises + the dependabot-specific evaluation
            GithubForgeWorkflow.PLAN_WRITTEN,
            GithubForgeWorkflow.TOKEN_ESTIMATED,
            UPGRADE_EVALUATED,
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Check out the Dependabot PR branch (`checkout-dependabot-pr`). Implement any "
            "recommended supporting changes from the plan (or none, if the plan recommended "
            "none). Implement any additional user requests or feedback. Keep tests green and "
            "push to the Dependabot PR branch. The user self-reviews the evaluation and the "
            "change and approves by advancing to MERGING."
        )
        responsibilities = (
            Responsibility(
                key="plan-implemented",
                description=(
                    "Any recommended supporting changes from the plan are implemented (or the "
                    "plan recommended none)."
                ),
            ),
            Responsibility(
                key="requests-implemented", description="All user requests are implemented in code."
            ),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(
                key="committed-pushed",
                description="Changes are committed and pushed to the Dependabot PR branch.",
            ),
            Responsibility(
                key="ci-passing",
                description="CI tests are passing, or any failures are irrelevant flakes.",
            ),
            GithubForgeWorkflow.URL_RECORDED,
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
        """Swap the inherited ``open-pr`` (the PR already exists) for ``checkout-dependabot-pr``,
        and reuse the inherited ``babysit-ci`` / ``babysit-merge`` verbatim."""
        forge = {skill.name: skill for skill in super().skills()}
        return (
            Skill(
                "checkout-dependabot-pr",
                "Check out the Dependabot PR branch and record its URL.",
                "The Dependabot PR already exists and its URL is the task memo.\n"
                "1. Read the PR URL from the task memo.\n"
                "2. Put the working tree on the PR's head branch with "
                "`gh pr checkout <url>` (run in `/workspace`). Commit and push any recommended "
                "supporting changes onto this branch — that updates the Dependabot PR itself.\n"
                "3. Call the `set_url` MCP tool with the PR URL so the dashboard's `p` hotkey "
                "opens it and the `url-recorded` responsibility can be resolved.",
            ),
            forge["babysit-ci"],
            forge["babysit-merge"],
        )
