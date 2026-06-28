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
                "**Overview — push-driven watch with cross-turn state artifact.**\n"
                "Use `run_in_background` to arm a non-blocking CI watcher, then surrender the "
                "turn. On re-invocation (when the watcher finishes) pick up from the state "
                "artifact. This avoids occupying a turn for the full duration of CI.\n\n"
                "**State artifact: `babysit-ci-state.json`** (task artifact store)\n"
                "Read with `ReadMcpResourceTool` (URI "
                "`panopticon://tasks/<task_id>/artifacts/babysit-ci-state.json`); write/update "
                "with the `put_artifact` MCP tool (`name=\"babysit-ci-state.json\"`).\n"
                "Fields: `started_at` (ISO timestamp — budget anchor), `head_sha` (PR HEAD SHA "
                "at start), `watch_bash_id` (background task ID, `null` when none), `retries` "
                "(`{check_name: count}` map), `completed` (`false` while active; `true` when "
                "done or bailed — the cleanup signal, since artifacts cannot be deleted).\n\n"
                "**First invocation** (artifact absent OR `completed == true`):\n"
                "1. Run `gh pr view <pr> --json state,headRefOid,mergeable,mergeStateStatus` "
                "and branch on the result:\n"
                "   - `state=MERGED` → report and stop.\n"
                "   - `state=CLOSED` → report as unexpected and stop.\n"
                "   - `mergeStateStatus=DIRTY` / `mergeable=CONFLICTING` → conflicts present. "
                "**Do not watch CI** — a conflicting PR has no mergeable commit and `--watch` "
                "blocks forever. Fetch base, merge/rebase onto the branch, fix trivial conflicts "
                "and push. Bail to the user on a non-trivial conflict. After pushing, restart "
                "from Step 1.\n"
                "   - `mergeStateStatus=BEHIND` → branch is behind base but not conflicting. "
                "Run `gh pr update-branch` or merge base locally, then restart from Step 1.\n"
                "   - `mergeStateStatus=BLOCKED` / `UNSTABLE` → a required check is failing or "
                "a review is blocking; surface the details and don't spin.\n"
                "   - `mergeStateStatus=UNKNOWN` → GitHub is still computing mergeability; wait "
                "briefly and retry Step 1 — don't treat as ready.\n"
                "   - `mergeStateStatus=CLEAN` / `HAS_HOOKS` → proceed.\n"
                "2. Write the state artifact: "
                "`{\"started_at\": \"<now ISO>\", \"head_sha\": \"<headRefOid>\", "
                "\"watch_bash_id\": null, \"retries\": {}, \"completed\": false}`.\n"
                "3. Arm the background watcher with `run_in_background`:\n"
                "   `gh pr checks <pr> --watch 2>&1 | tee /tmp/babysit-ci-watch.log; "
                "echo EXIT:$? >> /tmp/babysit-ci-watch.log`\n"
                "4. Update the state artifact with the returned bash task ID (`watch_bash_id`).\n"
                "5. End the turn. The stop hook keeps `turn=agent` while the background task is "
                "running and fires the agent back when the watcher completes.\n\n"
                "**Re-invocation** (artifact present AND `completed == false`):\n"
                "1. Read the state artifact. If `started_at` is more than 2 h ago → write "
                "`{..., \"completed\": true}` and bail to the user with a timeout message.\n"
                "2. If `watch_bash_id` is set and the background task is still running → end "
                "the turn immediately (spurious re-invocation; the watcher fires us again when "
                "done).\n"
                "3. Read `/tmp/babysit-ci-watch.log`. Parse the `EXIT:N` trailer at the end.\n"
                "4. If `EXIT:0` → all checks passed. Write `{..., \"completed\": true}` to the "
                "state artifact, report success, and stop (the stop hook flips turn to user — "
                "do not auto-advance).\n"
                "5. If `EXIT:` non-zero → extract failing check names from the log; increment "
                "their `retries` counters. If any counter exceeds 3 → write "
                "`{..., \"completed\": true}` and bail to the user. Otherwise: diagnose the "
                "failure, fix it in the worktree, commit and push. Overwrite the state artifact "
                "with fresh state (`watch_bash_id: null`, `completed: false`, updated `retries`, "
                "same `started_at`) to force a fresh watcher, then restart from First Invocation "
                "Step 1.\n\n"
                "**Anti-patterns to avoid:**\n"
                "- Never run `gh pr checks --watch` synchronously (blocking) — always wrap it "
                "in `run_in_background` as above. The **exit code** captured in the log is the "
                "pass/fail signal; never read the `conclusion` field to determine check status.\n"
                "- Never poll manually with a shell loop — use `run_in_background` + state "
                "artifact instead, or `gh run watch <run_id> --exit-status` for a single run.\n"
                "- Never use fixed-length SHA slices (`[:7]`, `[:8]`) for run matching — use "
                "`headSha.startswith(\"<short-sha>\")` and gate on `status == \"completed\"` "
                "before reading any result field.\n"
                "- Never grep `displayTitle` — GitHub rewrites it on PR rename and the pattern "
                "will match the wrong run.",
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
