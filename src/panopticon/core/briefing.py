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


def _ordered_phases(workflow: Workflow) -> list[str]:
    """The happy-path phase order: from the initial state, follow each state's `advance` edge until
    a terminal state (or a state with no `advance`). The lifecycle as a line, for the overview."""
    order: list[str] = []
    label: str | None = workflow.initial_label
    while label is not None and label not in order:
        order.append(label)
        if workflow.is_terminal(label):
            break
        label = workflow.operations(label).get("advance")
    return order


def render_workflow_overview(workflow: Workflow) -> str:
    """A one-time **map** of the whole workflow (the agent gets this in its system prompt): the
    ordered phases, what each is for, and how it advances. Static per workflow — the per-turn
    :func:`render_state_briefing` is the "you are here" pin on top of it."""
    lines = [
        f"# The `{workflow.name}` workflow",
        "",
        "This task moves through a fixed sequence of phases. You are always in exactly one phase: "
        "do that phase's work, then it advances. Each turn you'll be reminded which phase you're in "
        "and what it needs — **don't do a later phase's work early.** The phases, in order:",
        "",
    ]
    for i, label in enumerate(_ordered_phases(workflow), 1):
        if workflow.is_terminal(label):
            lines.append(f"{i}. **{label}** — terminal; the task is finished.")
            continue
        responsibilities = list(workflow.responsibilities(label))
        agent_advances = workflow.advanced_by(label) is Actor.AGENT
        if responsibilities:
            how = (
                "you advance it yourself once its responsibilities are met"
                if agent_advances
                else "you finish its responsibilities, then hand back to the user, who advances it"
            )
            lines.append(f"{i}. **{label}** — {how}:")
            lines += [f"   - {r.key}: {r.description}" for r in responsibilities]
        else:
            how = (
                "do the work, then advance it yourself"
                if agent_advances
                else "do the work, then hand back to the user, who advances it"
            )
            lines.append(f"{i}. **{label}** — {how}.")
    lines += [
        "",
        "Moving between phases: **`advance`** follows this sequence and is gated on the current "
        "phase's responsibilities; **`drop`** abandons the task (→ DROPPED) from anywhere; and if the "
        "user redirects you, you can move straight to any phase (a free move — e.g. back to an "
        "earlier phase to redo work).",
    ]

    tools = list(workflow.tools())
    if tools:
        lines += [
            "",
            "## Tools",
            "",
            "Beyond the usual shell (git, bash, …), this workflow's container has:",
        ]
        lines += [f"- `{t.name}` — {t.description}" for t in tools]

    return "\n".join(lines)


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
