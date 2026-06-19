"""SQLAlchemy store adapter (ADR 0001/0006: SQL behind the backend-agnostic interface).

One adapter serves every SQL backend SQLAlchemy speaks; **"in-memory" is just an in-memory
SQLite engine**. The pure, frozen domain models (:mod:`panopticon.core.models`) never touch
the ORM — this adapter owns mutable *row* classes and each knows how to translate itself
``to_domain`` / ``from_domain``. Parent→child links are ORM ``relationship``\\ s (loaded
eagerly via ``selectin``), so reading a task pulls in its history and responsibilities and
writing one cascades — no hand-written load/insert code.

The integrity checks are enforced by the base :class:`~panopticon.core.store.Store`
template methods; this adapter implements the persistence primitives. ``_update_task`` only
appends new entries and updates the current entry's promises in place — never rewriting
recorded history — rather than letting the unit-of-work write whatever is dirty.

Schema management: ``__init__`` calls ``metadata.create_all`` to bootstrap a fresh database
(zero-config dev + the ephemeral in-memory engine the tests use). Versioned evolution is owned
by **Alembic** (``migrations/``, ADR 0001 §3) — the initial migration reproduces this exact
schema, and ``tests/test_migrations.py`` guards the two against drift. On a persistent
deployment, run ``alembic upgrade head`` to apply migrations; ``alembic stamp head`` aligns a
dev database that ``create_all`` already bootstrapped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import JSON, ForeignKey, ForeignKeyConstraint, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from panopticon.core.models import Actor, HistoryEntry, Repo, Responsibility, Status, Task
from panopticon.core.store import (
    AlreadyExists,
    IntegrityError,
    NotFound,
    Store,
)

_IN_MEMORY = ("sqlite://", "sqlite:///:memory:")


# -- ORM row classes (mutable; live only in this adapter; own their translation) ----


class _Base(DeclarativeBase):
    pass


#: The schema's table metadata — Alembic's autogenerate target (``migrations/env.py``) and the
#: single source of truth migrations are checked against (``tests/test_migrations.py``).
metadata = _Base.metadata


class _RepoRow(_Base):
    __tablename__ = "repo"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    git_url: Mapped[str]
    default_base: Mapped[str]
    env_file: Mapped[str | None] = mapped_column(default=None)
    creds_volume: Mapped[str | None] = mapped_column(default=None)
    image_layer: Mapped[str | None] = mapped_column(default=None)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def to_domain(self) -> Repo:
        return Repo(
            id=self.id, name=self.name, git_url=self.git_url, default_base=self.default_base,
            env_file=self.env_file, creds_volume=self.creds_volume, image_layer=self.image_layer,
            capabilities=dict(self.capabilities or {}),
        )

    @classmethod
    def from_domain(cls, repo: Repo) -> _RepoRow:
        return cls(
            id=repo.id, name=repo.name, git_url=repo.git_url, default_base=repo.default_base,
            env_file=repo.env_file, creds_volume=repo.creds_volume, image_layer=repo.image_layer,
            capabilities=dict(repo.capabilities),
        )


class _TaskRow(_Base):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(primary_key=True)
    repo_id: Mapped[str] = mapped_column(ForeignKey("repo.id"))
    workflow: Mapped[str]
    state: Mapped[str]
    turn: Mapped[str]
    blocked: Mapped[bool] = mapped_column(default=False)
    slug: Mapped[str | None]
    branch: Mapped[str | None] = mapped_column(default=None)
    clone: Mapped[str | None] = mapped_column(default=None)
    claimed_by: Mapped[str | None] = mapped_column(default=None)
    history: Mapped[list[_HistoryRow]] = relationship(
        order_by="_HistoryRow.seq",
        cascade="all, delete-orphan",
        lazy="selectin",
        back_populates="task",
    )

    def to_domain(self) -> Task:
        return Task(
            id=self.id,
            repo_id=self.repo_id,
            workflow=self.workflow,
            state=self.state,
            turn=Actor(self.turn),
            blocked=self.blocked,
            slug=self.slug,
            branch=self.branch,
            clone=self.clone,
            claimed_by=self.claimed_by,
            history=[h.to_domain() for h in self.history],
        )

    @classmethod
    def from_domain(cls, task: Task) -> _TaskRow:
        return cls(
            id=task.id,
            repo_id=task.repo_id,
            workflow=task.workflow,
            state=task.state,
            turn=task.turn.value,
            blocked=task.blocked,
            slug=task.slug,
            branch=task.branch,
            clone=task.clone,
            claimed_by=task.claimed_by,
            history=[_HistoryRow.from_domain(e, seq) for seq, e in enumerate(task.history)],
        )


class _HistoryRow(_Base):
    __tablename__ = "history"

    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[str]
    from_state: Mapped[str | None]
    to_state: Mapped[str]
    trigger: Mapped[str | None]
    note: Mapped[str | None]
    task: Mapped[_TaskRow] = relationship(back_populates="history")
    responsibilities: Mapped[list[_ResponsibilityRow]] = relationship(
        order_by="_ResponsibilityRow.idx",
        cascade="all, delete-orphan",
        lazy="selectin",
        back_populates="history",
    )

    def to_domain(self) -> HistoryEntry:
        return HistoryEntry(
            at=self.at,
            from_state=self.from_state,
            to_state=self.to_state,
            trigger=self.trigger,
            note=self.note,
            responsibilities=[r.to_domain() for r in self.responsibilities],
        )

    @classmethod
    def from_domain(cls, entry: HistoryEntry, seq: int) -> _HistoryRow:
        # FK columns (task_id, and the child rows' task_id/seq) are filled by the relationships.
        return cls(
            seq=seq,
            at=entry.at,
            from_state=entry.from_state,
            to_state=entry.to_state,
            trigger=entry.trigger,
            note=entry.note,
            responsibilities=[
                _ResponsibilityRow.from_domain(r, idx)
                for idx, r in enumerate(entry.responsibilities)
            ],
        )


class _ResponsibilityRow(_Base):
    __tablename__ = "responsibility"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "seq"], ["history.task_id", "history.seq"]),
    )

    task_id: Mapped[str] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    idx: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str]
    description: Mapped[str]
    status: Mapped[str]
    comment: Mapped[str | None]
    history: Mapped[_HistoryRow] = relationship(back_populates="responsibilities")

    def to_domain(self) -> Responsibility:
        return Responsibility(
            key=self.key, description=self.description, status=Status(self.status), comment=self.comment
        )

    @classmethod
    def from_domain(cls, r: Responsibility, idx: int) -> _ResponsibilityRow:
        return cls(
            idx=idx, key=r.key, description=r.description, status=r.status.value, comment=r.comment
        )


def _fulfil_current_promises(history_row: _HistoryRow, entry: HistoryEntry) -> None:
    """Apply in-place fulfilment of the current entry's promises (status/comment updates only).

    The promise model never changes an entry's responsibility *set* — only each promise's
    status/comment — so this is a row-by-row update, no insert/delete. Guard the invariant
    since :func:`~panopticon.core.store.validate_history_append_only` doesn't police the
    current entry's responsibilities.
    """
    existing = history_row.responsibilities  # ordered by idx
    if [r.key for r in existing] != [r.key for r in entry.responsibilities]:
        raise IntegrityError("the current entry's responsibility set changed")
    for row, r in zip(existing, entry.responsibilities):
        row.status = r.status.value
        row.comment = r.comment


class SqlAlchemyStore(Store):
    """A :class:`~panopticon.core.store.Store` backed by SQLAlchemy."""

    def __init__(self, url: str = "sqlite://") -> None:
        if url in _IN_MEMORY:
            # An in-memory SQLite DB lives only as long as its single connection — pin one.
            self._engine = create_engine(
                url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        else:
            self._engine = create_engine(url)
        _Base.metadata.create_all(self._engine)
        self._session: sessionmaker[Session] = sessionmaker(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    # -- repos --------------------------------------------------------------------

    def _create_repo(self, repo: Repo) -> None:
        with self._session.begin() as s:
            if s.get(_RepoRow, repo.id) is not None:
                raise AlreadyExists(f"repo {repo.id!r} already exists")
            s.add(_RepoRow.from_domain(repo))

    def _get_repo(self, repo_id: str) -> Repo | None:
        with self._session() as s:
            row = s.get(_RepoRow, repo_id)
            return row.to_domain() if row is not None else None

    def _list_repos(self) -> list[Repo]:
        with self._session() as s:
            return [r.to_domain() for r in s.scalars(select(_RepoRow).order_by(_RepoRow.id))]

    # -- tasks: reads + persistence primitives (the base's template methods drive these) --

    def _get_task(self, task_id: str) -> Task | None:
        with self._session() as s:
            row = s.get(_TaskRow, task_id)
            return row.to_domain() if row is not None else None

    def _list_tasks(self) -> list[Task]:
        with self._session() as s:
            return [r.to_domain() for r in s.scalars(select(_TaskRow).order_by(_TaskRow.id))]

    def _create_task(self, task: Task) -> None:
        with self._session.begin() as s:
            if s.get(_TaskRow, task.id) is not None:
                raise AlreadyExists(f"task {task.id!r} already exists")
            if s.get(_RepoRow, task.repo_id) is None:
                raise NotFound(f"repo {task.repo_id!r} does not exist")
            s.add(_TaskRow.from_domain(task))  # cascade inserts history + responsibilities

    def _stored_history(self, task_id: str) -> list[HistoryEntry]:
        with self._session() as s:
            row = s.get(_TaskRow, task_id)
            if row is None:
                raise NotFound(f"task {task_id!r} does not exist")
            return [h.to_domain() for h in row.history]

    def _update_task(self, task: Task, stored: Sequence[HistoryEntry]) -> None:
        with self._session.begin() as s:
            row = s.get(_TaskRow, task.id)
            if row is None:  # defensive: single-writer, so it still exists after _stored_history
                raise NotFound(f"task {task.id!r} does not exist")
            row.state = task.state
            row.turn = task.turn.value
            row.blocked = task.blocked
            row.slug = task.slug
            row.branch = task.branch
            row.clone = task.clone
            row.claimed_by = task.claimed_by
            # The current (last stored) entry's promises may have been fulfilled in place.
            if stored:
                _fulfil_current_promises(row.history[len(stored) - 1], task.history[len(stored) - 1])
            # Append any new entries; the relationship cascade inserts them and their children.
            for seq in range(len(stored), len(task.history)):
                row.history.append(_HistoryRow.from_domain(task.history[seq], seq))
