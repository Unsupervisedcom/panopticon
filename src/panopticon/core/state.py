"""Workflow states, expressed as classes (declarative, à la an ORM model).

A state is a **class** used as a type-level identity (never instantiated). Subclass:

* :class:`State` — a non-terminal state. It carries an inherited transition to
  :class:`Dropped`, so every task is always droppable; concrete states add their own
  ``transitions`` (which accumulate with the inherited ``Dropped`` across the hierarchy).
* :class:`TerminalState` — a terminal state (no outgoing transitions).

``transitions`` entries are other state **classes** or their ``label`` **strings**; strings
are resolved to classes when the owning :class:`~panopticon.core.workflow.Workflow` is built
(forward references — e.g. cycles — must use strings, as with an ORM relationship).

Each state declares two orthogonal, immutable facts:

* ``turn_on_enter`` — who holds the turn *on entry* (distinct from ``Task.turn``, the live
  holder, which may differ later within the state);
* ``advanced_by`` — who moves the task *out* of the state (the user, or the agent once
  satisfied). These are independent: e.g. a plan state is left by the user (approval) yet
  the next state may begin on the agent's turn.
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from panopticon.core.models import Actor, Responsibility


class BaseState(ABC):
    """Common base for workflow states — use :class:`State` or :class:`TerminalState`."""

    #: Stable identifier — persisted in ``Task.state`` and shown on the dashboard.
    label: ClassVar[str]
    #: Who holds the turn upon entering this state.
    turn_on_enter: ClassVar[Actor]
    #: The agent's obligations while in this state (empty = ungated).
    responsibilities: ClassVar[tuple[Responsibility, ...]] = ()


class TerminalState(BaseState):
    """A terminal state: the task is finished here; no outgoing transitions.

    The turn returns to the user once a task is terminal.
    """

    turn_on_enter: ClassVar[Actor] = Actor.USER


class Complete(TerminalState):
    """Built-in terminal state for successfully finished tasks."""

    label: ClassVar[str] = "COMPLETE"


class Dropped(TerminalState):
    """Built-in terminal state for abandoned tasks. Reachable from every non-terminal state."""

    label: ClassVar[str] = "DROPPED"


class State(BaseState):
    """A non-terminal state.

    Concrete states set ``label``, optionally override ``turn_on_enter``/``advanced_by``, and
    add their own ``transitions``. The inherited transition to :class:`Dropped` makes every
    task droppable without each state having to declare it. Defaults suit the common case — the
    agent acts first on entry, then the user reviews and advances — so a state where the
    agent advances itself once satisfied overrides ``advanced_by = Actor.AGENT``.
    """

    turn_on_enter: ClassVar[Actor] = Actor.AGENT
    advanced_by: ClassVar[Actor] = Actor.USER
    transitions: ClassVar[tuple[type[BaseState] | str, ...]] = (Dropped,)
