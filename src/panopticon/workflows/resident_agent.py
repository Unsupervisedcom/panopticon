"""A deterministic forge-watched workflow delegated to a repo's resident agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from panopticon.core.artifacts import ArtifactStore
from panopticon.core.models import Actor, Responsibility, Task
from panopticon.core.state import Complete, InitialState, State
from panopticon.core.workflow import Workflow


def issue_title_and_body(
    task: Mapping[str, Any], issue_artifact: str | None = None
) -> tuple[str, str]:
    """Build an issue title/body from durable task data, with a searchable task marker."""
    memo = str(task.get("memo") or "")
    lines = memo.splitlines()
    title = (lines[0].strip() if lines else "") or f"Panopticon task {task['id']}"
    if issue_artifact is not None:
        body = issue_artifact
    elif task.get("initial_prompt"):
        body = str(task["initial_prompt"])
    else:
        body = "\n".join(lines[1:])
    footer = f"Panopticon task: {task['id']}"
    body = body.rstrip()
    return title, f"{body}\n\n{footer}" if body else footer


class ResidentAgent(Workflow):
    """Delegate implementation to a configured external resident and observe it on GitHub."""

    name = "resident-agent"
    opt_in = True
    runner_type = "forge"
    when_to_use = (
        "Delegate a change to the repo's resident agent: panopticon files a GitHub issue, "
        "assigns it to the resident, and tracks the resident's PR through code-owner review to merge. "
        "No container, no local agent."
    )
    poll_interval_seconds: ClassVar[int] = 60
    pr_timeout_seconds: ClassVar[int] = 6 * 3600
    ISSUE_ARTIFACT_NAME: ClassVar[str] = "issue.md"

    class Filing(InitialState):
        label = "FILING"
        description = "File and assign the resident's GitHub issue."
        advanced_by = Actor.AGENT
        responsibilities = (
            Responsibility(
                key="issue-filed",
                description=(
                    "The issue exists on the forge, is assigned to the repo's resident agent, "
                    "and its URL is recorded on the task."
                ),
            ),
        )
        transitions = ("IMPLEMENTING",)

    class Implementing(State):
        label = "IMPLEMENTING"
        description = "Wait for the resident agent to open a pull request for the issue."
        advanced_by = Actor.AGENT
        responsibilities = (
            Responsibility(
                key="pr-opened",
                description=(
                    "A pull request referencing the issue exists and its URL is recorded on the task."
                ),
            ),
        )
        transitions = ("REVIEW",)

    class Review(State):
        label = "REVIEW"
        description = "Observe readiness, code-owner review, and approval on the pull request."
        advanced_by = Actor.AGENT
        responsibilities = (
            Responsibility(key="pr-ready", description="The pull request is not a draft."),
            Responsibility(
                key="review-requested",
                description="A review has been requested from or submitted by a code owner.",
            ),
            Responsibility(
                key="pr-approved",
                description="The pull request's review decision is APPROVED.",
            ),
        )
        transitions = ("MERGING",)

    class Merging(State):
        label = "MERGING"
        description = "Wait for the approved pull request to merge."
        advanced_by = Actor.AGENT
        responsibilities = (
            Responsibility(key="pr-merged", description="The pull request is merged."),
        )
        transitions = (Complete,)

    initial = Filing

    def _overview_extras(self) -> Sequence[str]:
        return (
            "Panopticon runs no agent for this task. The repo's resident implements the issue and "
            "GitHub code owners review the pull request; the deterministic forge watcher advances it.",
            "The task URL is the issue until a pull request exists, then it is the pull request.",
        )

    async def _briefing_extras(self, task: Task, *, artifacts: ArtifactStore) -> Sequence[str]:
        del task, artifacts
        return (
            "No Panopticon agent runs for this task: the resident implements it and code owners review "
            "it on GitHub. The task URL is the issue until a pull request exists, then the pull request.",
        )
