"""The Orchestrator workflow — an agent that creates and pre-plans *other* tasks.

A single agent-driven state (`ORCHESTRATING → {COMPLETE, DROPPED}`, like :class:`~panopticon.
workflows.spike.Spike`) whose agent decomposes a high-level request into a batch of child
tasks and **seeds each one ready for the user to approve**: it creates the task, writes its
`plan.md` artifact, sets its slug, records a token estimate, marks the child's `plan-written`
and `token-estimated` responsibilities met, and hands the child's turn to the user. The user
then only has to review the plan and advance
(`PLANNING → ITERATING`). The motivating use is fanning out `github-self-reviewed` /
`github-peer-reviewed` tasks that arrive pre-planned.

The cross-task moves reuse the per-task MCP tools (which already accept any ``task_id``):
``set_slug``/``put_artifact``/``resolve_responsibility``/``set_turn``. The one capability an
ordinary agent lacks — **creating** a task (and discovering repos/workflows) — is exposed as
MCP tools gated to orchestration workflows; this workflow opts in via ``orchestrates = True``
(see :class:`~panopticon.core.workflow.Workflow`). The recipe lives in the ``spawn-task`` skill.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Skill
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow
from panopticon.workflows.github_forge import GithubForgeWorkflow

#: The per-child recipe the orchestrator's agent follows. Spelled out because it spans several
#: tools across *another* task's id, and the order matters (the gate clears only once the plan
#: artifact exists and its responsibility is met).
_SPAWN_TASK_INSTRUCTIONS = f"""\
Create one new task and leave it **pre-planned, ready for the user to approve**. Repeat per task
you want to spawn. Throughout, your *own* task id is shown below; the new task has its *own* id.

1. **Choose the workflow.** Pick the `workflow` (usually `github-self-reviewed` or
   `github-peer-reviewed`; `list_workflows` lists the valid names). New tasks are created in
   *your own* repo — this first iteration can't create tasks in another repo.
2. **Create it.** Call the `create_task` tool with:
   - `orchestrator_task_id` set to *your own* task id
   - `workflow`
   - `memo` — a **brief one-line label for the dashboard** (not a full description)
   - `initial_prompt="review your plan"` — prefilled into the child agent's input box on first
     spawn so it starts by reading the plan you wrote rather than re-planning
   - `artifacts={{"plan.md": "<full markdown plan>"}}` — write the plan **inside this call** so
     it exists before the spawner can ever pick up the task; the spawner finds it present
   Record the **new task's id** from the result.
3. **Name it.** `set_slug` on the new id with a short kebab-case slug.
4. **Estimate its cost.** `set_token_estimate` on the new id with your forecast of the total
   tokens *that* task will consume.
5. **Clear the planning gates.** `resolve_responsibility` on the new id with `key="plan-written"`,
   `status="met"`, then again with `key="token-estimated"`, `status="met"`.
6. **Hand it to the user.** `set_turn` on the new id with `turn="user"`.

The new task now sits in **PLANNING** with its plan written and the gate cleared — the user
approves it by advancing it to ITERATING. When its container starts, the agent sees
"review your plan" prefilled in its input box; `plan.md` is guaranteed to already exist
because it was written inside step 2. When you have spawned everything the request calls
for, hand back to the user — they mark this orchestrator task COMPLETE when satisfied.
"""


_REVIEW_TASK_INSTRUCTIONS = """\
Review a spawned task's change and either approve it or leave a `review.md` artifact on the task.
Pass the child task's id as the argument when invoking this skill.

1. **Get the task.** Call `get_task` with the child task id from `$ARGUMENTS`. Note its slug,
   state, URL (the PR link, if any), and memo.
2. **Read its plan.** Call `list_artifacts` on the child task id, then read its `plan.md`
   artifact (via the returned MCP URI) to understand what the task set out to do.
3. **Inspect the diff.** If the task has a URL (a PR link):
   - `gh pr view <URL>` — read the PR title and description.
   - `gh pr diff <URL>` — read the full diff.
4. **Assess.** Does the implementation match the plan? Are there correctness bugs, missing edge
   cases, or clear simplifications? Is scope appropriate (no extra changes beyond what was planned)?
5. **Decide — two outcomes only:**
   - **Agree:** If the change looks correct and complete, state your approval briefly in the
     conversation. No artifact is written to the child task.
   - **Have feedback:** If there are issues, write a `review.md` artifact to the *child task*:

     ```
     put_artifact(task_id=<child_task_id>, name="review.md", content=<findings>)
     ```

     Format `review.md` as:
     ```
     # Review — <slug>

     ## Must fix
     - <actionable finding>

     ## Suggestions
     - <optional, lower-priority finding>
     ```
     Omit a section if empty. Keep findings concrete and actionable (file + line where relevant).
"""


class Orchestrator(Workflow):
    """ORCHESTRATING → {COMPLETE, DROPPED}. Agent-driven, ungated, and allowed to create and
    pre-plan other tasks (``orchestrates = True``)."""

    name: ClassVar[str] = "orchestrator"
    orchestrates: ClassVar[bool] = True

    class Orchestrating(InitialState):
        label = "ORCHESTRATING"
        description = "Decompose the request into tasks; create and pre-plan each one for the user to approve."
        transitions = (Complete,)  # + DROPPED inherited from State; `advance` derives → COMPLETE

    initial = Orchestrating

    def skills(self) -> Sequence[Skill]:
        """``spawn-task`` seeds a new child task plan-ready; ``review-task`` reviews one."""
        return (
            Skill(
                "spawn-task",
                "Create a new task and seed it with a plan, ready for the user to approve.",
                _SPAWN_TASK_INSTRUCTIONS,
            ),
            Skill(
                "review-task",
                "Review a spawned task's change — approve it or leave a review.md artifact on the task.",
                _REVIEW_TASK_INSTRUCTIONS,
            ),
        )
