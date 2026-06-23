"""The workflow interface: an abstract base class each concrete workflow subclasses.

A workflow is **code, not data** (ADR 0004). Its states are :mod:`~panopticon.core.state`
classes nested inside the workflow class; they are discovered, their string transition
references resolved to classes, and the whole graph validated the first time the workflow is
queried — then cached (à la an ORM's lazy mapper-configuration step). The resolved graph
lives in the :attr:`Workflow._graph` cached property; the public methods answer state-machine
queries against it.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import ClassVar

from panopticon.core.artifacts import ArtifactStore
from panopticon.core.models import Actor, HistoryEntry, Responsibility, Skill, Task, Tool
from panopticon.core.state import BaseState, Complete, Dropped, InitialState, State, TerminalState

_ABSTRACT_BASES = (BaseState, State, TerminalState)


class InvalidWorkflow(Exception):
    """Raised when a workflow's states/transitions are inconsistent or unresolvable."""


class IllegalTransition(Exception):
    """Raised when a requested transition is not permitted for a task's current state."""


class ResponsibilitiesNotMet(Exception):
    """Raised when the turn is handed back before the state's responsibilities are resolved.

    A responsibility is unresolved if it is still ``PENDING``, or ``FAILED`` without a comment.
    """


def _nested_states(workflow_cls: type) -> Iterator[type[BaseState]]:
    """Yield the state classes nested in a workflow class, in definition order."""
    for value in vars(workflow_cls).values():
        if isinstance(value, type) and issubclass(value, BaseState) and value not in _ABSTRACT_BASES:
            yield value


def _accumulated_transitions(state_cls: type[BaseState]) -> Iterator[type[BaseState] | str]:
    """Yield the ``transitions`` declared at every level of a state's MRO.

    This is how the inherited ``Dropped`` (declared on :class:`State`) combines with a
    concrete state's own transitions.
    """
    for klass in state_cls.__mro__:
        declared = klass.__dict__.get("transitions")
        if declared:
            yield from declared


@dataclass(frozen=True)
class _Graph:
    """The resolved, validated state graph — built once and cached on the workflow."""

    states: dict[str, type[BaseState]]  # label -> state class
    transitions: dict[str, frozenset[str]]  # label -> reachable labels
    operations: dict[str, dict[str, str]]  # label -> {operation name -> dest label}
    initial: str  # label of the initial state


class Workflow(ABC):
    """Abstract base for a workflow definition (the workflow interface)."""

    #: Stable identifier used to register and select the workflow.
    name: ClassVar[str]
    #: The state a new task starts in — a nested ``State`` class or its label string.
    initial: ClassVar[type[BaseState] | str]
    #: Whether this workflow's in-container agent may **orchestrate other tasks** — create new
    #: tasks (in its own repo) and discover the available workflows through the control plane. The
    #: orchestration MCP tools are gated to workflows that opt in (the task service checks this
    #: flag for the acting task); default off, so an ordinary workflow's agent can mutate only
    #: tasks it already knows, never create them.
    orchestrates: ClassVar[bool] = False

    # -- build / validate (the resolution pass; answers "why not a free function?") -----

    @cached_property
    def _graph(self) -> _Graph:
        """Discover, resolve, and validate this workflow's states — once, then cached."""
        by_label: dict[str, type[BaseState]] = {}

        def register(cls: type[BaseState]) -> None:
            if cls in _ABSTRACT_BASES:
                raise InvalidWorkflow(
                    f"{self.name!r}: abstract base {cls.__name__} used as a state"
                )
            label = getattr(cls, "label", None)
            if not label:
                raise InvalidWorkflow(f"{self.name!r}: state {cls.__name__} has no label")
            existing = by_label.get(label)
            if existing is not None and existing is not cls:
                raise InvalidWorkflow(f"{self.name!r}: duplicate state label {label!r}")
            by_label[label] = cls

        for cls in _nested_states(type(self)):
            register(cls)
        # Built-in terminals are always available (so "DROPPED"/"COMPLETE" resolve, and the
        # required Dropped state is always present).
        register(Complete)
        register(Dropped)

        def label_of(target: type[BaseState] | str) -> str:
            if isinstance(target, str):
                if target not in by_label:
                    raise InvalidWorkflow(
                        f"{self.name!r}: reference to unknown state {target!r}"
                    )
                return target
            if not (isinstance(target, type) and issubclass(target, BaseState)):
                raise InvalidWorkflow(f"{self.name!r}: invalid transition target {target!r}")
            if target.label not in by_label:
                register(target)  # a directly-referenced class not nested (e.g. a built-in)
            return target.label

        states: dict[str, type[BaseState]] = {}
        transitions: dict[str, frozenset[str]] = {}
        for label, cls in list(by_label.items()):
            if getattr(cls, "turn_on_enter", None) is None:
                raise InvalidWorkflow(f"{self.name!r}: state {label!r} has no turn_on_enter")
            if issubclass(cls, TerminalState):
                dests: frozenset[str] = frozenset()
            else:
                dests = frozenset(label_of(t) for t in _accumulated_transitions(cls))
            states[label] = cls
            transitions[label] = dests

        operations = {label: self._resolve_operations(cls, transitions[label]) for label, cls in states.items()}

        initial = label_of(self.initial)
        if not issubclass(states[initial], InitialState):
            raise InvalidWorkflow(
                f"{self.name!r}: initial state {initial!r} must subclass InitialState"
            )
        if "DROPPED" not in states:  # guaranteed by the built-in; assert the invariant
            raise InvalidWorkflow(f"{self.name!r}: a DROPPED terminal state is required")
        return _Graph(states=states, transitions=transitions, operations=operations, initial=initial)

    def _resolve_operations(self, cls: type[BaseState], dests: frozenset[str]) -> dict[str, str]:
        """The named core operations available from one state (verb -> dest label).

        Operations are named verbs for the **declared, gated** graph: `drop` (-> DROPPED) is
        implicit for every non-terminal state; `advance` is auto-derived as the single
        non-DROPPED transition (the happy path) — so a linear state declares nothing; a declared
        operation must target a legal transition. Off-graph moves are not operations — they are
        free moves (`set_state` / `force_transition`), the user's authority exercised through an
        agent skill.
        """
        if issubclass(cls, TerminalState):
            return {}
        ops: dict[str, str] = {}
        for name, target in getattr(cls, "operations", {}).items():
            dest = target if isinstance(target, str) else target.label
            if dest not in dests:
                raise InvalidWorkflow(
                    f"{self.name!r}: operation {name!r} on {cls.label!r} targets {dest!r}, "
                    "which is not one of its transitions"
                )
            ops[name] = dest
        if "advance" not in ops:
            forward = [d for d in dests if d != Dropped.label]
            if len(forward) == 1:
                ops["advance"] = forward[0]
        ops.setdefault("drop", Dropped.label)
        return ops

    # -- queries ----------------------------------------------------------------------

    def _state_class(self, label: str) -> type[BaseState]:
        try:
            return self._graph.states[label]
        except KeyError:
            raise InvalidWorkflow(f"{self.name!r}: unknown state {label!r}") from None

    @property
    def initial_label(self) -> str:
        return self._graph.initial

    def labels(self) -> Iterator[str]:
        """Yield all state labels (declared order, then built-in terminals)."""
        yield from self._graph.states

    def transitions(self, label: str) -> Iterator[str]:
        """Yield the labels reachable directly from ``label`` — its resolved legal transitions."""
        self._state_class(label)  # validate the label exists
        yield from self._graph.transitions[label]

    def operations(self, label: str) -> dict[str, str]:
        """The named core operations available from ``label`` — verb → destination label.

        Always includes `drop`; includes `advance` when there's a single forward edge or one
        was declared; plus any other workflow-declared operations. Terminal states offer none.
        """
        self._state_class(label)  # validate the label exists
        return dict(self._graph.operations[label])

    def resolve_operation(self, label: str, operation: str) -> str:
        """The destination label for ``operation`` from ``label``, or raise if unavailable."""
        self._state_class(label)  # validate the label exists
        try:
            return self._graph.operations[label][operation]
        except KeyError:
            raise IllegalTransition(
                f"{self.name!r}: operation {operation!r} is not available in state {label!r}"
            ) from None

    def can_transition(self, source: str, dest: str) -> bool:
        self._state_class(source)  # validate the label exists
        return dest in self._graph.transitions[source]

    def is_terminal(self, label: str) -> bool:
        return issubclass(self._state_class(label), TerminalState)

    def description(self, label: str) -> str:
        """The state's human-facing description — what the phase is *for* (may be empty)."""
        return self._state_class(label).description

    def turn_on_enter(self, label: str) -> Actor:
        """Who holds the turn upon entering ``label`` (the state's declared value)."""
        return self._state_class(label).turn_on_enter

    def advanced_by(self, label: str) -> Actor:
        """Who moves the task out of ``label`` — the user, or the agent once satisfied."""
        cls = self._state_class(label)
        if not issubclass(cls, State):
            raise InvalidWorkflow(f"{self.name!r}: terminal state {label!r} does not advance")
        return cls.advanced_by

    def responsibilities(self, label: str) -> Iterator[Responsibility]:
        """Yield the obligations (PENDING definitions) the agent takes on entering ``label``."""
        yield from self._state_class(label).responsibilities

    def skills(self) -> Sequence[Skill]:
        """Workflow-specific in-container skills (ADR 0004), on top of the core operations.

        Declared as agnostic :class:`~panopticon.core.models.Skill` specs; the agent layer
        renders them to the active CLI's surface. Skills are optional — they are whatever extra
        agent procedures a workflow wants to offer (the github-peer-reviewed workflow's forge
        skills are one example, not a
        requirement), so the base default is none; a workflow overrides this to declare its own.
        """
        return ()

    def tools(self) -> Sequence[Tool]:
        """Command-line tools this workflow's container provides beyond the base shell/git — named
        so the agent's system prompt can tell it what to use (e.g. github-peer-reviewed's `gh`). Declared as data;
        the *install* is :meth:`image_layer`. Default none; a workflow overrides this."""
        return ()

    def image_layer(self) -> str:
        """The workflow's Docker image layer (ADR 0005): a Dockerfile fragment appended on top of
        the base image with what this workflow's skills need (e.g. `gh` for forge). Default none;
        the runner composes base → workflow → repo into the task's image."""
        return ""

    # -- lifecycle hooks (deterministic; run in the control plane, no LLM) ---------------

    def on_transition(
        self, task: Task, *, from_state: str | None, to_state: str, artifacts: ArtifactStore
    ) -> None:
        """Hook run by the task service after a transition is applied, before persistence.

        The default is a no-op. Overrides may write artifacts (e.g. seed the plan on plan
        acceptance) or mutate the task's own record. Deterministic — no LLM, no clock; any
        timestamps come from the task/history already stamped by the caller.
        """

    def provision(self, task: Task, *, branch: str, worktree_path: str) -> None:
        """Workflow provisioning, run **after** the core creates the task's slug-named worktree
        (ADR 0004 / ARCHITECTURE §9). Default no-op.

        *Local* git (the branch + worktree) is core and already done by the time this runs;
        this seam is for workflow-specific *remote* setup that needs the branch — e.g. the
        github-peer-reviewed workflow opening its PR — which is forge integration and lands in a later slice.
        Deterministic forge requests may run here; agent-driven forge work is an in-container
        skill (the determinism split, ADR 0004).
        """

    # -- task lifecycle (deterministic: no clock, no I/O; timestamps passed in) ---------

    def _promised(self, label: str) -> list[Responsibility]:
        """A fresh PENDING responsibility list to seed the history entry for entering ``label``."""
        return list(self.responsibilities(label))

    def start_task(
        self, task_id: str, repo_id: str, *, at: str, description: str | None = None
    ) -> Task:
        """Create a task in this workflow's initial state, with turn and seed history set.

        The seed history entry carries the initial state's responsibilities (all ``PENDING``).
        ``description`` is the optional free-text intent collected at creation.
        """
        state = self.initial_label
        return Task(
            id=task_id,
            repo_id=repo_id,
            workflow=self.name,
            state=state,
            turn=self.turn_on_enter(state),
            description=description,
            history=[
                HistoryEntry(
                    at=at,
                    from_state=None,
                    to_state=state,
                    trigger="start",
                    responsibilities=self._promised(state),
                )
            ],
        )

    def apply_transition(
        self,
        task: Task,
        to_state: str,
        *,
        at: str,
        trigger: str | None = None,
        note: str | None = None,
    ) -> Task:
        """Validate and apply a transition, mutating ``task`` in place and returning it.

        Enforces, in order: the task is not terminal; the transition is legal; and, unless
        this is a **drop**, that every responsibility promised on entering the *current* state
        is resolved (each ``MET`` or ``FAILED``-with-comment, none ``PENDING``). Dropping is
        always allowed and bypasses the gate. On success, appends a new history entry for the
        destination state — seeded with *its* responsibilities (``PENDING``) — and recomputes
        the turn.
        """
        if self.is_terminal(task.state):
            raise IllegalTransition(f"task {task.id!r} is terminal ({task.state!r})")
        if not self.can_transition(task.state, to_state):
            raise IllegalTransition(f"{self.name!r}: no transition {task.state!r} -> {to_state!r}")
        # Dropping is the universal escape hatch — always allowed, never gated.
        if to_state != Dropped.label:
            outstanding = task.outstanding_responsibilities
            if outstanding:
                raise ResponsibilitiesNotMet(
                    f"{self.name!r}: responsibility {outstanding[0].key!r} is not resolved"
                )
        return self._enter(task, to_state, at=at, trigger=trigger, note=note)

    def force_transition(
        self,
        task: Task,
        to_state: str,
        *,
        at: str,
        trigger: str | None = None,
        note: str | None = None,
    ) -> Task:
        """Set the task to *any* state directly — the user's authority to move freely.

        Bypasses the declared graph and the responsibility gate (and the terminal check), so the
        user can correct or override the lifecycle from anywhere to anywhere (this is why a
        backward edge like REVIEW→ITERATING need not be a declared transition). Validates only
        that the target state exists. Same history/turn bookkeeping as a normal transition.
        """
        self._state_class(to_state)  # validate the target exists
        return self._enter(task, to_state, at=at, trigger=trigger, note=note)

    def _enter(self, task: Task, to_state: str, *, at: str, trigger: str | None, note: str | None) -> Task:
        """Append the entry for ``to_state`` (seeding its promises) and recompute state + turn."""
        task.history.append(
            HistoryEntry(
                at=at,
                from_state=task.state,
                to_state=to_state,
                trigger=trigger,
                note=note,
                responsibilities=self._promised(to_state),
            )
        )
        task.state = to_state
        task.turn = self.turn_on_enter(to_state)
        return task
