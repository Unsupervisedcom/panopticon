"""The `redo` skill (agnostic; exposed on every task) — restart the current state's work.

Sometimes a state's work needs to start over from scratch: the plan was wrong, the iteration
went sideways, the responsibilities should come back unmet. ``redo`` re-enters the task's
*current* state — re-seeding every responsibility as ``PENDING``, re-applying ``turn_on_enter``,
and starting a fresh turn — without leaving the state (the engine primitive is
``Workflow.redo``). It's a free move (ungated), distinct from ``advance`` (gated, forward) and
``drop`` (the escape).

This is data — a skill spec — so it lives in ``core`` (LLM-free): the task service exposes it on
every task (`TaskService.skills`), and the container's agent layer renders it to the active CLI
surface, like the `provision` skill.
"""

from __future__ import annotations

from panopticon.core.models import Skill

#: The skill's name — shared by the skill spec and the MCP/REST `redo` verb so they can't drift.
REDO_SKILL_NAME = "redo"

REDO_SKILL = Skill(
    REDO_SKILL_NAME,
    "Re-enter the current state from scratch: reset its responsibilities and start a fresh turn.",
    "Restart the current state's work when it should begin again from scratch (e.g. the plan or "
    "the iteration went wrong and its obligations should come back unmet). Invoke it with the "
    "`redo` tool (`task_id=\"...\"`); don't edit the state directly. It **re-enters the current "
    "state**: every responsibility is reset to pending, the turn is handed back to you, and a new "
    "turn begins — you stay in the same state, you don't advance or drop. Use it deliberately; it "
    "discards your progress on the current state's responsibilities.",
)
