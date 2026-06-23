"""The Orchestrator workflow — an agent that creates and pre-plans *other* tasks.

A single agent-driven state (`ORCHESTRATING → {COMPLETE, DROPPED}`, like :class:`~panopticon.
workflows.spike.Spike`) whose agent decomposes a high-level request into a batch of child
tasks and **seeds each one ready for the user to approve**: it creates the task, writes its
`plan.md` artifact, sets its slug, marks the child's `plan-written` responsibility met, and
hands the child's turn to the user. The user then only has to review the plan and advance
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
2. **Create it.** Call the `create_task` tool with `orchestrator_task_id` set to *your own* task
   id, plus `workflow`, and a `description` (put the intent/provenance here). Record the **new
   task's id** from the result.
3. **Name it.** `set_slug` on the new id with a short kebab-case slug.
4. **Write its plan.** `put_artifact` on the new id with `name="{GithubForgeWorkflow.PLAN_ARTIFACT_NAME}"` and the full
   markdown plan for *that* task.
5. **Clear the plan gate.** `resolve_responsibility` on the new id with `key="plan-written"`,
   `status="met"`.
6. **Hand it to the user.** `set_turn` on the new id with `turn="user"`.

The new task now sits in **PLANNING** with its plan written and the gate cleared — the user
approves it by advancing it to ITERATING. (A container will later spawn for it; because
`plan-written` is already met and the turn is the user's, its own agent will hand straight back
rather than re-plan.) When you have spawned everything the request calls for, hand back to the
user — they mark this orchestrator task COMPLETE when satisfied.
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
        """The ``spawn-task`` skill: the recipe for creating one task and seeding it plan-ready."""
        return (
            Skill(
                "spawn-task",
                "Create a new task and seed it with a plan, ready for the user to approve.",
                _SPAWN_TASK_INSTRUCTIONS,
            ),
        )
