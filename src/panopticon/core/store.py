"""The store interface: the abstraction over persisted task state.

A backend-agnostic interface (ADR 0001/0006). The task service is its sole owner and the
single writer; a SQLAlchemy adapter implements it (SQLite — in-memory or on-disk — in this
slice; other SQL backends later).

Integrity rules — the "transition enforcement at the boundary":

* a task's history is non-empty and ``state`` equals the last entry's ``to_state``
  (``validate_task_consistency``), checked on create *and* save;
* on save, history is **append-only**: the stored history is a prefix of the supplied one and
  recorded transition facts never change (``validate_history_append_only``).

These are *enforced by the base class*: every public method delegates to a ``_``-prefixed
primitive an adapter implements, and ``create_task`` / ``save_task`` run the checks before
delegating to ``_create_task`` / ``_stored_history`` / ``_update_task`` — so no adapter can
persist without the checks running.

The *legality* of a transition (which state may follow which) is decided by the engine
before save; the store guarantees the persisted record stays internally consistent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from panopticon.core.models import HistoryEntry, Repo, Task


class StoreError(Exception):
    """Base class for store failures."""


class NotFound(StoreError):
    """Raised when an entity referenced by id does not exist."""


class AlreadyExists(StoreError):
    """Raised when creating an entity whose id is already taken."""


class IntegrityError(StoreError):
    """Raised when a write would violate an integrity rule (e.g. non-append-only history)."""


class Store(ABC):
    """Persistence boundary for repos and tasks.

    The public methods are concrete and **delegate to the ``_``-prefixed primitives** an
    adapter implements — so every overridable method is underscored and cross-cutting rules
    live in one place. ``create_task`` / ``save_task`` additionally run the integrity checks
    (``validate_task_consistency`` / ``validate_history_append_only``) before delegating, so an
    adapter can't skip them.

    **Change feed (single-writer seam).** The store carries a monotonically increasing
    :meth:`version`, bumped after every task mutation (create/save). Callers can therefore tell
    "nothing changed" from "something did" without diffing snapshots, and a registered
    :meth:`subscribe` listener is invoked (synchronously) on each bump — the seam the HTTP layer
    drives a block-until-change long-poll from. The counter is a plain integer, not a clock, and
    the listeners are plain sync callbacks, so ``core`` stays clock-free and LLM-free; any async
    push / timeout lives in the HTTP layer, not here.
    """

    def __init__(self) -> None:
        self._version = 0
        self._change_listeners: list[Callable[[], None]] = []

    # -- change feed (the block-until-change seam) --------------------------------

    def version(self) -> int:
        """A counter bumped on every task mutation; ``0`` before any write. Monotonic, so a
        caller can long-poll for "the version moved past what I last saw" (see :meth:`subscribe`)."""
        return self._version

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Register a callback invoked (synchronously) after every task mutation. The HTTP layer
        subscribes an async-broadcast wake-up so a ``GET /tasks`` long-poll returns the instant a
        task changes. Listeners must not raise and must not block."""
        self._change_listeners.append(listener)

    def _bump_version(self) -> None:
        """Advance the version and wake subscribers — called by the task-write façade methods."""
        self._version += 1
        for listener in self._change_listeners:
            listener()

    # -- repos (public façade) ----------------------------------------------------

    def create_repo(self, repo: Repo) -> None:
        """Persist a new repo. Raises :class:`AlreadyExists` if its id is taken."""
        self._create_repo(repo)

    def get_repo(self, repo_id: str) -> Repo | None:
        """Return the repo, or ``None`` if it does not exist."""
        return self._get_repo(repo_id)

    def list_repos(self) -> list[Repo]:
        """Return all repos."""
        return self._list_repos()

    def update_repo(self, repo: Repo) -> None:
        """Persist changes to an existing repo (a full-row write). Raises :class:`NotFound`
        if no repo with its id exists. The *merge* of a partial update is the caller's job
        (the service reads-modifies-writes); the store just overwrites the row."""
        self._update_repo(repo)

    # -- tasks (public façade; create/save also enforce the integrity rules) ------

    def create_task(self, task: Task) -> None:
        """Persist a new task and its initial history, after checking consistency."""
        validate_task_consistency(task)
        self._create_task(task)
        self._bump_version()

    def get_task(self, task_id: str) -> Task | None:
        """Return the task (with full history), or ``None`` if it does not exist."""
        return self._get_task(task_id)

    def list_tasks(self) -> list[Task]:
        """Return all tasks (with full history)."""
        return self._list_tasks()

    def save_task(self, task: Task) -> None:
        """Persist an updated task, enforcing consistency and append-only history."""
        validate_task_consistency(task)
        stored = self._stored_history(task.id)
        validate_history_append_only(stored, task.history)
        self._update_task(task, stored)
        self._bump_version()

    # -- persistence primitives (adapters implement these) -----------------------

    @abstractmethod
    def _create_repo(self, repo: Repo) -> None:
        """Insert a new repo. Raise :class:`AlreadyExists` if its id is taken."""

    @abstractmethod
    def _get_repo(self, repo_id: str) -> Repo | None:
        """Return the repo, or ``None``."""

    @abstractmethod
    def _list_repos(self) -> list[Repo]:
        """Return all repos."""

    @abstractmethod
    def _update_repo(self, repo: Repo) -> None:
        """Overwrite an existing repo's row. Raise :class:`NotFound` if its id is unknown."""

    @abstractmethod
    def _create_task(self, task: Task) -> None:
        """Insert a new task + its history. Raise :class:`AlreadyExists` if the id is taken,
        :class:`NotFound` if its ``repo_id`` does not exist."""

    @abstractmethod
    def _get_task(self, task_id: str) -> Task | None:
        """Return the task (with full history), or ``None``."""

    @abstractmethod
    def _list_tasks(self) -> list[Task]:
        """Return all tasks (with full history)."""

    @abstractmethod
    def _stored_history(self, task_id: str) -> list[HistoryEntry]:
        """Return the task's persisted history. Raise :class:`NotFound` if it does not exist."""

    @abstractmethod
    def _update_task(self, task: Task, stored: Sequence[HistoryEntry]) -> None:
        """Persist scalar changes, fulfil the current entry's promises, and append new entries
        (``stored`` is the already-validated persisted history)."""


# -- Shared integrity checks (adapters call these so the rules live in one place) --------


def validate_task_consistency(task: Task) -> None:
    """Check a task is internally consistent: non-empty history, state matches its tail."""
    if not task.history:
        raise IntegrityError(f"task {task.id!r} has empty history")
    if task.state != task.history[-1].to_state:
        raise IntegrityError(
            f"task {task.id!r}: state {task.state!r} != last history to_state "
            f"{task.history[-1].to_state!r}"
        )


def _transition_facts(entry: HistoryEntry) -> tuple[str, str | None, str, str | None, str | None]:
    """An entry's transition facts — everything that is immutable once recorded."""
    return (entry.at, entry.from_state, entry.to_state, entry.trigger, entry.note)


def validate_history_append_only(
    stored: Sequence[HistoryEntry], incoming: Sequence[HistoryEntry]
) -> None:
    """Check ``incoming`` only extends ``stored``.

    Transition facts are immutable for every recorded entry. The sole permitted in-place
    change is the **current (last) entry's responsibilities**, which the agent fulfils over
    the course of that turn (the promise-on-entry model); once an entry is followed by another
    it is frozen.
    """
    if len(incoming) < len(stored):
        raise IntegrityError("history shrank (not append-only)")
    for i, prev in enumerate(stored):
        cur = incoming[i]
        if _transition_facts(prev) != _transition_facts(cur):
            raise IntegrityError("existing history was modified (not append-only)")
        # Only the current entry's promises may still change; earlier entries are final.
        if i < len(stored) - 1 and list(prev.responsibilities) != list(cur.responsibilities):
            raise IntegrityError("a finalized entry's responsibilities were modified")
