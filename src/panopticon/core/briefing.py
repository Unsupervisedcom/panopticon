"""The per-turn **state briefing** — what the agent is told about *where it is* in the workflow.

A workflow is a state machine, but the agent only sees a flat set of skills + the `advance`/`drop`
operations; nothing tells it which phase it's in or what that phase is *for*. So it can charge ahead
— e.g. start implementing during a parity task's PLANNING phase. This renders a short, workflow-**a
gnostic** briefing from the current state's metadata: the phase, its responsibilities (the work to
do here), and whether to hand back to the user or advance once they're met. The container's
user-prompt hook prints it each turn (like the provisioning nudge), so it's in the agent's context
from its first action.

Pure data → string (LLM-free, lives in `core`); the task service renders it, the hook emits it.
"""

from __future__ import annotations

from panopticon.core.models import Actor, Task
from panopticon.core.workflow import Workflow


def render_state_briefing(workflow: Workflow, task: Task) -> str:
    """A short briefing on the task's current phase: its responsibilities and how it advances."""
    label = task.state
    if workflow.is_terminal(label):
        return f"This task is in the terminal state **{label}** — it's finished; there's nothing to do."

    lines = [
        f"You are in the **{label}** phase of the `{workflow.name}` workflow. Do the work this phase "
        f"calls for and then hand back — **don't start work that belongs to a later phase.**"
    ]

    responsibilities = list(task.current_entry.responsibilities)
    if responsibilities:
        lines += ["", "This phase's responsibilities (resolve each before advancing):"]
        lines += [f"- [{r.status.value}] {r.key}: {r.description}" for r in responsibilities]

    target = workflow.operations(label).get("advance")
    if target is not None:
        lines.append("")
        if workflow.advanced_by(label) is Actor.USER:
            lines.append(
                f"When these are met, **stop and hand back to the user** — they review and decide "
                f"when to advance (→ {target}). Don't advance on your own."
            )
        else:
            lines.append(f"When these are met, advance the task yourself (the `advance` operation → {target}).")
    return "\n".join(lines)
