"""Abstract base for workflows that produce a plan artifact.

`PlannedWorkflow` carries the plan convention shared by any lifecycle that asks the agent
to write a plan before coding: the canonical artifact name, the shared PLANNING
plan responsibility, the plan URI resolver, and the per-turn briefing hook that surfaces the
plan's MCP URI once it exists. Concrete subclasses add a ``name`` and their states.

Both :class:`~panopticon.workflows.github_forge.GithubForgeWorkflow` (GitHub-hosted code)
and :class:`~panopticon.workflows.local_git_self_reviewed.LocalGitSelfReviewed` (local git,
no forge) inherit from here — keeping the plan convention single-sourced.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.artifacts import ArtifactStore, mcp_uri
from panopticon.core.models import Responsibility, Task
from panopticon.core.workflow import Workflow


class PlannedWorkflow(Workflow):
    """Abstract base for workflows with a plan artifact. Not registrable on its own (no
    ``name``, no states) — only concrete subclasses are discovered and registered."""

    #: The canonical artifact name for the plan. Written by the agent via ``put_artifact``
    #: during PLANNING; read back at :meth:`plan_uri`. Kept as a constant so the
    #: PLAN_WRITTEN responsibility description and the dashboard `a` hotkey stay in sync.
    PLAN_ARTIFACT_NAME: ClassVar[str] = "plan.md"

    #: Shared PLANNING responsibility: upload the plan as a ``plan.md`` artifact (not a
    #: working-tree file) so the operator can open it from the dashboard, and summarize it in
    #: chat when ending the planning turn so the user can review without opening the artifact.
    PLAN_WRITTEN: ClassVar[Responsibility] = Responsibility(
        key="plan-written",
        description=(
            f"The plan is uploaded to the plan artifact `{PLAN_ARTIFACT_NAME}` (a markdown file) with "
            "the `put_artifact` tool — not just written to the working tree. When you end your "
            "planning turn, also post a concise summary of the plan in chat so the user can review "
            "it without opening the artifact."
        ),
    )

    @classmethod
    def plan_uri(cls, task_id: str) -> str:
        """The canonical MCP resource URI for a task's plan artifact.

        Surfaced in the state briefing so the agent reads the plan back at exactly this URI
        instead of guessing (e.g. an orchestrator-spawned agent handed a pre-written plan).
        """
        return mcp_uri(task_id, cls.PLAN_ARTIFACT_NAME)

    async def _briefing_extras(self, task: Task, *, artifacts: ArtifactStore) -> Sequence[str]:
        """Once the plan artifact exists, surface its canonical MCP URI in the per-turn
        briefing. Gated on existence so a still-to-be-planned PLANNING turn doesn't point
        at a missing file."""
        if self.PLAN_ARTIFACT_NAME not in await artifacts.list(task.id):
            return ()
        return [
            f"This task's plan is the `{self.PLAN_ARTIFACT_NAME}` artifact — read it at this exact MCP "
            f"resource URI: `{self.plan_uri(task.id)}` (don't guess the URI)."
        ]
