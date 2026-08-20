"""The GithubDependabot workflow — evaluate and land a Dependabot dependency-bump PR.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). The same collapsed
graph as :class:`~panopticon.workflows.github_self_reviewed.GithubSelfReviewed` (no peer
`REVIEW` state — the user self-reviews the evaluation and approves the bump by advancing out of
`ITERATING`), specialised for Dependabot:

- The **PR already exists** — Dependabot opened it, and the task's **memo is a link to that PR**.
  Its URL and its checked-out tree are therefore **inputs**, established during PLANNING, not
  ITERATING outputs. There is nothing to *open*; instead of the inherited ``open-pr`` skill, this
  workflow provides a ``checkout-dependabot-pr`` skill that (once the task is provisioned) puts the
  working tree on the PR's head branch (``gh pr checkout``) and records its URL — run during
  PLANNING so the evaluation happens against the actual upgraded tree. Any recommended supporting
  changes are then committed and pushed onto that same branch during ITERATING, updating the
  Dependabot PR. Provisioning is unchanged (the ``panopticon/<slug>`` branch is simply unused), so
  ``core``/``taskservice``/``sessionservice`` need no changes.
- **PLANNING produces an evaluation**, not an implementation plan. The plan (`plan.md`) must
  evaluate the five points captured by :data:`GithubDependabot.UPGRADE_EVALUATED`: how the
  upgraded module is used, how relevant the change is to this repo, how risky it is (semver scope
  + breaking changes), whether it addresses a security advisory (and how urgent), and any
  recommended supporting changes — *none is an acceptable answer*. Because the PR URL is a known
  input (the memo), the shared ``url-recorded`` responsibility is gated here too — recorded in
  PLANNING via ``checkout-dependabot-pr``, not in ITERATING.

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
#: all five axes. On top of the shared PLAN_WRITTEN (plan is a markdown artifact),
#: so the evaluation is a gated checkbox — not merely implied by a plan existing.
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

#: MERGING responsibility specific to the dependency-bump lifecycle: the bump PR is approved (via
#: `approve-dependabot-pr`) so branch protection's required-review gate is satisfied and the bump
#: can land. Gated as an explicit, dashboard-visible promise rather than folded into the shared
#: `babysit-merge` skill. Dependabot authored the PR, so the agent's token — a different identity —
#: may approve it; this is scoped to this workflow (on the self/peer-reviewed lifecycles the agent
#: is effectively the author and must not self-approve). Module scope for the same reason as
#: UPGRADE_EVALUATED: the nested `Merging` body can't see the enclosing class's namespace.
PR_APPROVED = Responsibility(
    key="pr-approved",
    description=(
        "The Dependabot bump PR is approved (`gh pr review --approve`, via `approve-dependabot-pr`) "
        "so branch protection's required-review gate is satisfied and the bump can land."
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

    #: Re-export of the module-level MERGING approval responsibility (see :data:`PR_APPROVED`).
    PR_APPROVED: ClassVar[Responsibility] = PR_APPROVED

    class Planning(InitialState):
        label = "PLANNING"
        description = (
            "Name the task (`/provision`), then run `checkout-dependabot-pr` to put the working "
            "tree on the linked PR's head branch and record its URL — the PR URL (the task memo) "
            "and its checked-out tree are inputs to the evaluation, not outputs. Read the PR (its "
            "diff, changelog, and release notes) and produce a plan (`plan.md`) that evaluates the "
            "bump on five axes against that checked-out tree: how the upgraded module is used in "
            "this repo, how relevant the change is to that usage, how risky it is (semver scope + "
            "breaking changes — a build/test spot-check against the upgraded tree is the strongest "
            "risk signal), whether it addresses a security advisory / CVE and how urgent it is, "
            "and any recommended supporting changes (concluding that none are needed is acceptable)."
        )
        responsibilities = (  # shared plan promise + the dependabot-specific evaluation +
            GithubForgeWorkflow.PLAN_WRITTEN,  # the PR URL, a known input (the memo), recorded here
            UPGRADE_EVALUATED,
            GithubForgeWorkflow.URL_RECORDED,
        )
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "The PR branch is already checked out from PLANNING (re-run `checkout-dependabot-pr` "
            "if the container was respawned). Implement any recommended supporting changes from "
            "the plan (or none, if the plan recommended none). Implement any additional user "
            "requests or feedback. Keep tests green and push to the Dependabot PR branch. The user "
            "self-reviews the evaluation and the change and approves by advancing to MERGING."
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
        )  # url-recorded is gated in PLANNING (the URL is an input — the memo — not an output)
        transitions = ("MERGING",)  # no REVIEW: the user self-reviews, then advances to MERGING

    class Merging(State):
        label = "MERGING"
        description = (
            "First run `approve-dependabot-pr` to approve the bump PR (Dependabot authored it, so "
            "the agent's token is a different identity and may approve it) — this satisfies branch "
            "protection's required review so the bump can land. Then add the PR to the merge queue "
            "with `babysit-merge`; if the PR exits the merge queue, re-add it."
        )
        advanced_by = Actor.AGENT  # background: the agent shepherds the merge and advances itself
        responsibilities = (
            # The bump PR is approved so branch protection's required-review gate is satisfied.
            # Gated here (not folded into `babysit-merge`) so the approval is an explicit, dashboard-
            # visible promise the agent must resolve — not a line of prose it could skim past.
            PR_APPROVED,
            Responsibility(key="pr-merged", description="The PR is merged."),
        )
        transitions = (Complete,)  # the happy path; `advance` derives → COMPLETE

    initial = Planning

    def skills(self) -> Sequence[Skill]:
        """Swap the inherited ``open-pr`` (the PR already exists) for ``checkout-dependabot-pr``,
        add the dependabot-only ``approve-dependabot-pr`` (MERGING's ``pr-approved`` gate), and
        reuse the inherited ``babysit-ci`` / ``babysit-merge`` verbatim."""
        forge = {skill.name: skill for skill in super().skills()}
        return (
            Skill(
                "checkout-dependabot-pr",
                "Check out the Dependabot PR branch (for evaluation) and record its URL.",
                "The Dependabot PR already exists and its URL is the task memo — its URL and "
                "checked-out tree are inputs. Run this during PLANNING so you evaluate the bump "
                "against the actual upgraded tree.\n"
                "1. Make sure the task is provisioned first (`/provision` — set the slug) so "
                "`origin` points at the forge; `gh pr checkout` needs the forge remote.\n"
                "2. Read the PR URL from the task memo.\n"
                "3. Put the working tree on the PR's head branch with "
                "`gh pr checkout <url>` (run in `/workspace`), so the evaluation and any "
                "build/test spot-check of the bump run against the upgraded tree. During ITERATING, "
                "commit and push any recommended supporting changes onto this branch — that updates "
                "the Dependabot PR itself. (If a fresh container starts in ITERATING, re-run this "
                "to put the tree back on the PR branch.)\n"
                "4. Call the `set_url` MCP tool with the PR URL so the dashboard's `p` hotkey "
                "opens it and the `url-recorded` responsibility can be resolved.",
            ),
            Skill(
                "approve-dependabot-pr",
                "Approve the Dependabot bump PR so branch protection's required review is met.",
                "Run this **once at the start of MERGING**, before `babysit-merge` queues the PR — "
                "an approving review is what satisfies branch protection so the bump can land.\n"
                "Dependabot is the PR author and the agent's token is a *different* identity, so it "
                "may approve the PR (this is not a self-approval, and is scoped to this "
                "dependency-bump workflow).\n"
                "1. Read the PR URL from the task memo (or the recorded task URL).\n"
                "2. Approve it: `gh pr review <url> --approve` (run in `/workspace`). If it reports "
                "the PR is already approved by you, that's fine — treat it as done.\n"
                "3. Resolve the `pr-approved` responsibility (`resolve_responsibility`, MET), then "
                "run `babysit-merge` to shepherd the PR through the merge queue.",
            ),
            forge["babysit-ci"],
            forge["babysit-merge"],
        )
