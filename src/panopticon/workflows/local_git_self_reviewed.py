"""The LocalGitSelfReviewed workflow — a GitHub-free, forge-free lifecycle.

`PLANNING → ITERATING → MERGING → COMPLETE` (plus the inherited `DROPPED`). For repos where the
work never becomes a pull request — no PR, no CI pipeline, no remote merge queue. The agent
implements and commits locally; the user reviews the diff themselves and approves the work by
advancing `ITERATING → MERGING`; the agent then merges the task branch into the base branch (via
the `local-merge` skill) and **pushes the result back to the repo the task was cloned from**
before advancing itself to `COMPLETE`.

That push is not something the container can do. A per-task clone's ``origin`` is the repo's
``git_url`` verbatim, which for a local repo is a path on the *host* — and only the clone itself is
mounted into the container, so that path isn't there. So the agent asks (``request_push``) and the
session service's :class:`~panopticon.sessionservice.publisher.Publisher` performs the git where
the clone actually lives, reporting the outcome back onto the task. Without it the merge would only
ever land in a throwaway per-task checkout and the operator's repo would never see it.

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
    self-reviews** and approves by advancing to MERGING, then the agent merges the branch
    and has it pushed back to the repo the task was cloned from. No forge dependency."""

    name: ClassVar[str] = "local-git-self-reviewed"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "No PR and no CI — use when the work never needs to become a pull request; you approve "
        "the diff, and the agent merges the task branch into your repo's base branch and pushes "
        "it back to the repo the task was cloned from."
    )

    class Planning(InitialState):
        label = "PLANNING"
        description = "Collect requirements. Produce a plan for the implementation."
        responsibilities = (PlannedWorkflow.PLAN_WRITTEN,)
        transitions = ("ITERATING",)  # advance; + DROPPED inherited

    class Iterating(State):
        label = "ITERATING"
        description = (
            "Implement the plan. Implement any additional user requests or feedback. "
            "The user self-reviews and approves the change by advancing to MERGING."
        )
        responsibilities = (
            Responsibility(key="plan-implemented", description="The plan is implemented in code."),
            Responsibility(
                key="requests-implemented", description="All user requests are implemented in code."
            ),
            Responsibility(key="tests-pass", description="New and relevant tests pass locally."),
            Responsibility(
                key="committed", description="Changes are committed to the local branch."
            ),
        )
        transitions = ("MERGING",)  # the user self-reviews, then advances to MERGING

    class Merging(State):
        label = "MERGING"
        description = (
            "Merge the task branch into the repo's base branch and push the result back to origin."
        )
        advanced_by = Actor.AGENT  # background: agent drives the merge and advances itself
        responsibilities = (
            # The key stays `local-merged` even though the obligation now covers the push:
            # responsibilities are seeded onto the history record when a task *enters* MERGING, so
            # renaming it would leave every in-flight task unable to resolve what it promised.
            Responsibility(
                key="local-merged",
                description=(
                    "Changes are merged into the repo's base branch and pushed to origin."
                ),
            ),
        )
        transitions = (Complete,)

    initial = Planning

    def skills(self) -> Sequence[Skill]:
        return (
            Skill(
                "local-merge",
                "Merge the task branch into the base branch and push it back to origin.",
                "## 1. Find the branches\n\n"
                "The task branch is `git -C /workspace branch --show-current`.\n\n"
                "**Detect the base branch — never assume `main`.** In order, stopping at the "
                "first that works:\n"
                "1. `git -C /workspace symbolic-ref --short refs/remotes/origin/HEAD` "
                "(strip the leading `origin/`).\n"
                "2. `git -C /workspace remote set-head origin --auto`, then retry step 1.\n"
                "3. Whichever of `origin/main` / `origin/master` exists in "
                "`git -C /workspace branch --remotes`.\n\n"
                "If none of those resolve, stop and ask the user which branch to merge into — "
                "do not guess.\n\n"
                "## 2. Merge\n\n"
                "`git -C /workspace checkout <base>` then `git -C /workspace merge --no-ff "
                "<task-branch>`. If there are merge conflicts, go back to coding "
                "(`set_state ITERATING`) with an explanation of what conflicted.\n\n"
                "## 3. Push it back to origin\n\n"
                "The merge so far exists only in this container's checkout. **Do not run `git "
                "push` yourself** — origin is a path on the host, which does not exist in here. "
                "Call the `request_push` MCP tool with the base branch; the session service "
                "pushes the task branch (a backup) and then the base branch, on the host.\n\n"
                "Then poll `get_task` every few seconds (give up after ~60s and tell the user) "
                "until the `push` field's `status` is no longer `requested`:\n\n"
                "- **`pushed`** — both branches landed. Resolve `local-merged` and advance to "
                "COMPLETE.\n"
                "- **`partial`** — the task branch landed but the base branch was refused; "
                "`detail` says why and how to fix it. The work is safe in the user's repo, so "
                "there is nothing to re-implement: `set_state ITERATING` and relay `detail` "
                "verbatim, telling them you can retry the push on their word (repeat step 3 "
                "alone — do not re-merge).\n"
                "- **`failed`** — nothing landed. If `detail` says origin moved on (not a "
                "fast-forward), you may `git -C /workspace fetch origin` and merge "
                "`origin/<base>` into the base branch; if that merges cleanly, call "
                "`request_push` once more. On a conflict, a second failure, or any other "
                "`detail`, `set_state ITERATING` and relay `detail` verbatim.\n\n"
                "Only advance to COMPLETE on `pushed` — the `local-merged` responsibility covers "
                "the push, not just the merge.",
            ),
        )
