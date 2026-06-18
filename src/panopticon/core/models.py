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


@dataclass(frozen=True)
class Skill:
    """A workflow-specific, agent-driven procedure exposed in the task container (ADR 0004).

    Workflow-agnostic by design: a workflow declares skills as data (``name``, one-line
    ``description``, and ``instructions`` — the agent procedure); the agent layer renders them to
    whatever the active CLI needs (claude slash-commands for now, other CLIs in M3). On top of
    the core operations (advance/drop), and present only if the active workflow defines them.
    """

    name: str
    description: str
    instructions: str


@dataclass
class Repo:
    """A repository tasks operate on.

    Holds *references* to its per-repo secrets (ADR 0007), never the values: ``env_file`` is a
    host path to an env-file of API-key-style secrets, and ``creds_volume`` names a persisted
    volume of OAuth credential files. Both are injected into the task container at launch (the
    runner), so secrets stay out of the DB, artifacts, and image layers.

    ``image_layer`` is the repo's Dockerfile fragment (ADR 0005's repo tier): the runner composes
    base → workflow → **repo** into the task image, so a repo can layer on its toolchain (e.g. `uv`,
    `make`). Empty/None = no repo layer.
    """

    id: str
    name: str
    git_url: str
    default_base: str = "main"
    env_file: str | None = None
    creds_volume: str | None = None
    image_layer: str | None = None


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
    #: A deliberate "waiting on something" marker the agent sets; it is **orthogonal to the
    #: turn** and survives turn flips (cloude-cade's `:blocked:`), cleared only explicitly.
    blocked: bool = False
    slug: str | None = None
    #: The git refs the session service provisions for this task once the slug is set (ADR
    #: 0010/0011): the slug-named branch and the path of the per-task ``clone`` it works in **on
    #: the host where the container runs**. The task service only records these — it does no git
    #: itself — so this stays correct when the runner is remote. Both ``None`` until provisioning.
    branch: str | None = None
    clone: str | None = None
    #: The runner that has **claimed** this task (its ``runner_id``), or ``None`` if unclaimed. A
    #: session service claims an unclaimed task before spawning its container, so exactly one host
    #: owns it; the claim is the spawn gate (ADR 0008). Released (back to ``None``) to hand it off
    #: or have it respawned. Distinct from liveness — a claimed task whose container died is
    #: "claimed but down".
    claimed_by: str | None = None
    history: list[HistoryEntry] = field(default_factory=list)

    @property
    def provisioned(self) -> bool:
        """True once the session service has provisioned this task — its branch (and per-task
        clone) are recorded (ADR 0011). Until then the task has at most a slug, no working branch.
        """
        return self.branch is not None

    @property
    def current_entry(self) -> HistoryEntry:
        """The latest history entry — the one recorded on entering the current state."""
        return self.history[-1]

    def resolve_responsibility(
        self, *, key: str, status: Status, comment: str | None = None
    ) -> None:
        """Resolve one responsibility promised on entering the current state, in place.

        Resolves the matching promise on :attr:`current_entry`. ``status`` must be ``MET`` or
        ``FAILED`` (the latter requires a ``comment``). Raises :class:`ValueError` for an
        unknown key, a ``PENDING`` status, or a ``FAILED`` without a comment.
        """
        if status is Status.PENDING:
            raise ValueError("resolve a responsibility as MET or FAILED, not PENDING")
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
        ``FAILED`` promise counts as resolved — :meth:`resolve_responsibility` already requires
        its comment, so it never lingers here.
        """
        return [r for r in self.current_entry.responsibilities if r.status is Status.PENDING]
