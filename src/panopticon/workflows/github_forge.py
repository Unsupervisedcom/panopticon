"""Shared base for the GitHub-forge workflows (ADR 0004, ADR 0005).

`GithubForgeWorkflow` carries everything common to workflows whose code reaches GitHub and
whose lifecycle is shepherded through a PR: the `gh` tool the agent reaches for, the image
layer that installs it, and the forge skills (`open-pr`, `babysit-ci`, `babysit-merge`) the
agent drives against `gh`/CI. The concrete lifecycles differ only in their **states** — a
peer gates the merge (`GithubPeerReviewed`) or the user self-reviews and approves it
(`GithubSelfReviewed`) — so each subclass supplies its own `name` + states and inherits the
forge plumbing from here.

The plan convention (artifact name, shared responsibilities, URI resolver, briefing hook)
lives on :class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`; this class extends
it and adds the GitHub-specific layer (``gh`` tool, image layer, forge skills).

This base is **abstract**: it declares no `name` value and no states, so workflow discovery
(`workflows.discovery`) never registers or instantiates it — it keeps only classes with a
string `name` defined in the scanned module.
"""

from __future__ import annotations

from collections.abc import Sequence

from panopticon.core.models import Skill, Tool
from panopticon.workflows.planned_workflow import PlannedWorkflow


class GithubForgeWorkflow(PlannedWorkflow):
    """Abstract base for GitHub-forge workflows: shared `gh` tool, image layer, and forge
    skills. Concrete subclasses add a ``name`` and their states; they inherit the plumbing
    below. Not a registrable workflow on its own (no ``name``, no states).

    The plan convention (``PLAN_ARTIFACT_NAME``, ``PLAN_WRITTEN``, ``TOKEN_ESTIMATED``,
    :meth:`plan_uri`, :meth:`_briefing_extras`) is inherited from
    :class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`."""

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
                "**Step 1 — check merge state before touching CI.**\n"
                "Run `gh pr view <pr> --json mergeable,mergeStateStatus,statusCheckRollup` and "
                "branch on `mergeStateStatus`:\n"
                "- `DIRTY` / `mergeable=CONFLICTING` → the PR has conflicts. **Do not call "
                "`gh pr checks --watch`** — a conflicting PR has no mergeable commit so GitHub "
                "produces no check runs and `--watch` blocks forever. Instead: fetch the base, "
                "merge/rebase it onto the branch, fix trivial conflicts and push. Bail to the user "
                "on a non-trivial conflict (resolving conflicts is part of this skill, not the "
                "user's job). After pushing, go back to Step 1.\n"
                "- `BEHIND` → the branch is behind base but not conflicting. Update it "
                "(`gh pr update-branch` or merge base locally), then go back to Step 1.\n"
                "- `BLOCKED` / `UNSTABLE` → a required check is failing or a review is blocking; "
                "surface the details and don't spin.\n"
                "- `UNKNOWN` → GitHub is still computing mergeability; wait a moment and retry "
                "Step 1 — don't treat as ready.\n"
                "- `CLEAN` / `HAS_HOOKS` → checks are present and running; proceed to Step 2.\n\n"
                "**Step 2 — watch CI (only once the PR is clean/mergeable).**\n"
                "Per failing check: rerun obvious flakes (don't count them), else diagnose, fix "
                "in the worktree, and commit + push. After each push, **go back to Step 1** to "
                "re-verify merge state before watching again (the base can move during CI). "
                "Budget: ≤3 post-fix retries per check and ~2h wall-clock. Stop when CI is green "
                "— report and hand back to the user (don't auto-advance) — or when the budget is "
                "spent.\n\n"
                "**CI-watch: use the blocking commands — do not hand-roll a polling loop.**\n"
                "- Preferred: `gh pr checks --watch` (blocks until all PR checks settle; non-zero "
                "on failure) or `gh run watch <run_id> --exit-status` (blocks until that run "
                "finishes). No shell loop, no SHA arithmetic.\n"
                "- If polling is ever unavoidable: match runs with "
                "`headSha.startswith(\"<short-sha>\")` — never a fixed-length slice (`[:7]`, "
                "`[:8]`) which silently mismatches — **and** gate on `status == \"completed\"` "
                "before reading `conclusion`.\n"
                "- Never grep `displayTitle`: GitHub rewrites it on PR rename and the pattern "
                "will match the wrong (renamed-old) run.\n"
                "- Never treat a run appearing in the list as done — only `conclusion` tells "
                "you the result.",
            ),
            Skill(
                "babysit-merge",
                "Shepherd the PR through the merge queue.",
                "Before queuing, confirm the PR is mergeable: "
                "`gh pr view <pr> --json mergeable,mergeStateStatus` — if `DIRTY` (conflicts) or "
                "`BEHIND`, fix that first (use `babysit-ci` or resolve manually) before adding to "
                "the merge queue. A `DIRTY` PR will be immediately ejected.\n\n"
                "Add the PR to the merge queue (`gh pr merge --squash --auto`, or the repo's "
                "policy) and watch it, re-queuing on transient ejections within a ~2h budget. If "
                "the merge is blocked — a failing required check, requested changes, or a conflict "
                "— go back to coding (`set_state ITERATING`) with an explanation. Once the merge "
                "has landed, advance to COMPLETE.",
            ),
        )
