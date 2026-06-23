"""Shared base for the GitHub-forge workflows (ADR 0004, ADR 0005).

`GithubForgeWorkflow` carries everything common to workflows whose code reaches GitHub and
whose lifecycle is shepherded through a PR: the `gh` tool the agent reaches for, the image
layer that installs it, and the forge skills (`open-pr`, `babysit-ci`, `babysit-merge`) the
agent drives against `gh`/CI. The concrete lifecycles differ only in their **states** — a
peer gates the merge (`GithubPeerReviewed`) or the user self-reviews and approves it
(`GithubSelfReviewed`) — so each subclass supplies its own `name` + states and inherits the
forge plumbing from here.

This base is **abstract**: it declares no `name` value and no states, so workflow discovery
(`workflows.discovery`) never registers or instantiates it — it keeps only classes with a
string `name` defined in the scanned module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.artifacts import ArtifactStore, mcp_uri
from panopticon.core.models import Responsibility, Skill, Task, Tool
from panopticon.core.workflow import Workflow


class GithubForgeWorkflow(Workflow):
    """Abstract base for GitHub-forge workflows: shared `gh` tool, image layer, and forge
    skills. Concrete subclasses add a ``name`` and their states; they inherit the plumbing
    below. Not a registrable workflow on its own (no ``name``, no states).

    Owns the **plan convention** for the forge lifecycles: the plan is a markdown
    :attr:`PLAN_ARTIFACT_NAME` artifact (not a working-tree file), read back at
    :meth:`plan_uri`. Single-sourced here — the only workflows with a plan gate — and referenced
    by the subclasses' PLANNING state and the orchestrator's spawn recipe."""

    #: The canonical artifact name for a forge task's plan. By convention the plan is a markdown
    #: ``plan.md`` **artifact** — uploaded with ``put_artifact``, not just written to ``/workspace``
    #: — so the operator can open it from the dashboard (the `a` hotkey keys off the extension).
    PLAN_ARTIFACT_NAME: ClassVar[str] = "plan.md"

    #: The shared PLANNING responsibility for the forge workflows. Both lifecycles produce a plan;
    #: one frozen instance, referenced by each workflow's PLANNING state, keeps the guidance
    #: single-sourced (wording keyed off :attr:`PLAN_ARTIFACT_NAME`).
    PLAN_WRITTEN: ClassVar[Responsibility] = Responsibility(
        key="plan-written",
        description=(
            f"The plan is uploaded to the plan artifact `{PLAN_ARTIFACT_NAME}` (a markdown file) with "
            "the `put_artifact` tool — not just written to the working tree."
        ),
    )

    @classmethod
    def plan_uri(cls, task_id: str) -> str:
        """The canonical MCP resource URI for a task's plan artifact (:attr:`PLAN_ARTIFACT_NAME`).

        The one URI an agent should read the plan back at — surfaced in the state briefing so
        orchestrator-spawned agents don't guess (e.g. ``artifact://<id>/plan.md`` → "Unknown resource").
        """
        return mcp_uri(task_id, cls.PLAN_ARTIFACT_NAME)

    def tools(self) -> Sequence[Tool]:
        """`gh` is in the image (see `image_layer`); name it so the agent reaches for it."""
        return (
            Tool(
                "gh",
                "the GitHub CLI — authenticated to the forge. Use it for all remote VCS: open and "
                "update the PR (`gh pr ...`), watch CI (`gh pr checks`), and merge. The forge skills "
                "drive it.",
            ),
        )

    def image_layer(self) -> str:
        """The forge skills shell out to `gh`, so layer it onto the base image (ADR 0005)."""
        return "RUN apt-get update && apt-get install --yes --no-install-recommends gh"

    def _briefing_extras(self, task: Task, *, artifacts: ArtifactStore) -> Sequence[str]:
        """Once the plan artifact exists, surface its canonical MCP URI in the per-turn briefing so
        the agent reads the plan back at the right URI instead of guessing (e.g. an orchestrator-
        spawned agent handed a pre-written plan — ``artifact://<id>/plan.md`` → "Unknown resource").
        Gated on existence so a still-to-be-planned PLANNING turn doesn't point at a missing file."""
        if self.PLAN_ARTIFACT_NAME not in artifacts.list(task.id):
            return ()
        return [
            f"This task's plan is the `{self.PLAN_ARTIFACT_NAME}` artifact — read it at this exact MCP "
            f"resource URI: `{self.plan_uri(task.id)}` (don't guess the URI)."
        ]

    def skills(self) -> Sequence[Skill]:
        """The forge skills (ADR 0004 — remote VCS is workflow-specific). The agent runs these
        in the container against `gh`/CI, calling back over MCP/REST."""
        return (
            Skill(
                "open-pr",
                "Open a draft PR for this task's branch.",
                "Push the task's branch and open a **draft** PR against the repo's base branch with "
                f"`gh pr create --draft`. Title it for the change and reference the plan artifact "
                f"(`{self.PLAN_ARTIFACT_NAME}`). "
                "Then record the PR's URL on the task with the `set_url` tool, so the dashboard's "
                "`p` hotkey opens it.",
            ),
            Skill(
                "babysit-ci",
                "Watch the PR's CI and fix failures (and base conflicts) until green.",
                "Watch the PR's checks (`gh pr checks --watch`). First resolve any merge conflict "
                "against the base — fetch and merge the base, fix trivial conflicts and push, but "
                "bail to the user on a non-trivial one (resolving conflicts is part of this skill, "
                "not the user's job). Then, per failing check: rerun obvious flakes (don't count "
                "them), else diagnose, fix in the worktree, and commit + push. Budget: ≤3 post-fix "
                "retries per check and ~2h wall-clock. Stop when CI is green — report and hand back "
                "to the user (don't auto-advance) — or when the budget is spent.",
            ),
            Skill(
                "babysit-merge",
                "Shepherd the PR through the merge queue.",
                "Add the PR to the merge queue (`gh pr merge --squash --auto`, or the repo's "
                "policy) and watch it, re-queuing on transient ejections within a ~2h budget. If "
                "the merge is blocked — a failing required check, requested changes, or a conflict "
                "— go back to coding (`set_state ITERATING`) with an explanation. Once the merge "
                "has landed, advance to COMPLETE.",
            ),
        )
