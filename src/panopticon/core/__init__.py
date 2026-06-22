"""Core domain: models, state classes, and the workflow interface (the state machine).

Nothing in this package performs I/O or calls an LLM (the determinism invariant — all LLM
calls happen inside task containers).
"""

from panopticon.core.models import (
    Actor,
    HistoryEntry,
    Repo,
    Responsibility,
    Skill,
    Status,
    Task,
    Tool,
)
from panopticon.core.state import (
    TERMINAL_LABELS,
    BaseState,
    Complete,
    Dropped,
    State,
    TerminalState,
)
from panopticon.core.workflow import (
    IllegalTransition,
    InvalidWorkflow,
    ResponsibilitiesNotMet,
    Workflow,
)

__all__ = [
    "Actor",
    "BaseState",
    "Complete",
    "Dropped",
    "HistoryEntry",
    "IllegalTransition",
    "InvalidWorkflow",
    "Repo",
    "ResponsibilitiesNotMet",
    "Responsibility",
    "Skill",
    "State",
    "Status",
    "TERMINAL_LABELS",
    "Task",
    "TerminalState",
    "Tool",
    "Workflow",
]
