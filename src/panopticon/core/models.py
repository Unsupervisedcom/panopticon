"""Core domain models — pure data, no I/O, no LLM.

These types are the vocabulary the whole system shares. Most are plain records; the exception
is :class:`Task`, which carries behavior over **its own record** — fulfilling the
responsibilities it promised on entry and reporting which remain outstanding. The *rules of
the state machine* (which transitions are legal, what each state means) live in
:class:`panopticon.core.workflow.Workflow`, and the state classes live in
:mod:`panopticon.core.state`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Actor(str, Enum):
    """A party that can act on a task: the user or the agent.

    The same two parties answer every "who?" in the model — who holds the turn
    (``Task.turn``, ``State.turn_on_enter``) and who transitions out of a state
    (``State.advanced_by``).
    """

    USER = "user"
    AGENT = "agent"


class Status(str, Enum):
    """Resolution status of a single responsibility."""

    PENDING = "pending"  # not yet resolved — blocks handing the turn back
    MET = "met"
    FAILED = "failed"  # could not be satisfied; requires a comment


@dataclass(frozen=True)
class Responsibility:
    """An agent obligation for a state.

    The workflow supplies these as *definitions* (``status`` ``PENDING``, no comment). The
    agent resolves each to ``MET`` or ``FAILED`` before handing the turn back; a ``FAILED``
    responsibility must carry a ``comment`` explaining why. They are agent-only — user
    actions drive transitions directly rather than being modelled as responsibilities.
    """

    key: str
    description: str
    status: Status = Status.PENDING
    comment: str | None = None

    def resolve(self, status: Status, comment: str | None = None) -> Responsibility:
        """Return a resolved copy carrying the definition's ``key``/``description``."""
        return Responsibility(
            key=self.key, description=self.description, status=status, comment=comment
        )


@dataclass
class Repo:
    """A repository tasks operate on. Owns secret references (added in a later slice)."""

    id: str
    name: str
    git_url: str
    default_base: str = "main"


@dataclass(frozen=True)
class HistoryEntry:
    """One entry in a task's log — recorded when the task *enters* ``to_state``.

    Timestamps are passed in by the caller (the task service stamps them); the core
    never reads the clock, which keeps the state machine deterministic and testable.

    On entry, ``responsibilities`` is seeded with the destination state's obligations, all
    ``PENDING`` — a promise to fulfil them before leaving. The agent then resolves them **one
    at a time**, which replaces entries in this list in place; that is the *only* mutable part
    of an otherwise append-only, frozen record (the transition facts never change).
    """

    at: str  # ISO-8601 timestamp, supplied by the caller
    from_state: str | None
    to_state: str
    trigger: str | None = None  # what triggered the transition (e.g. "start", "advance")
    note: str | None = None
    responsibilities: list[Responsibility] = field(default_factory=list)


@dataclass
class Task:
    """A unit of work. Identity is the internal ``id``; ``slug`` is a human label set later.

    A task carries behavior over **its own record** — fulfilling the responsibilities it
    promised on entering its current state, and reporting which remain outstanding. It knows
    nothing of the state machine's rules; those live in
    :class:`~panopticon.core.workflow.Workflow`, which drives the task across states.
    """

    id: str
    repo_id: str
    workflow: str
    state: str
    turn: Actor
    slug: str | None = None
    history: list[HistoryEntry] = field(default_factory=list)

    @property
    def current_entry(self) -> HistoryEntry:
        """The latest history entry — the one recorded on entering the current state."""
        return self.history[-1]

    def record_responsibility(
        self, *, key: str, status: Status, comment: str | None = None
    ) -> None:
        """Fulfil one responsibility promised on entering the current state, in place.

        Resolves the matching promise on :attr:`current_entry`. ``status`` must be ``MET`` or
        ``FAILED`` (the latter requires a ``comment``). Raises :class:`ValueError` for an
        unknown key, a ``PENDING`` status, or a ``FAILED`` without a comment.
        """
        if status is Status.PENDING:
            raise ValueError("record a responsibility as MET or FAILED, not PENDING")
        if status is Status.FAILED and not (comment and comment.strip()):
            raise ValueError(f"FAILED responsibility {key!r} requires a comment")
        promised = self.current_entry.responsibilities
        for i, definition in enumerate(promised):
            if definition.key == key:
                promised[i] = definition.resolve(status, comment)
                return
        raise ValueError(f"no responsibility {key!r} promised in state {self.state!r}")

    @property
    def outstanding_responsibilities(self) -> list[Responsibility]:
        """Promises on the current entry still unresolved (``PENDING``).

        An empty result means the turn may be handed back and the task may advance. A
        ``FAILED`` promise counts as resolved — :meth:`record_responsibility` already requires
        its comment, so it never lingers here.
        """
        return [r for r in self.current_entry.responsibilities if r.status is Status.PENDING]
