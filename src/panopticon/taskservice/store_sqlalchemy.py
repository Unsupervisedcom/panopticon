"""SQLAlchemy store adapter (ADR 0001/0006: SQL behind the backend-agnostic interface).

One adapter serves every SQL backend SQLAlchemy speaks; **"in-memory" is just an in-memory
SQLite engine**. The pure, frozen domain models (:mod:`panopticon.core.models`) never touch
the ORM — this adapter owns mutable *row* classes and each knows how to translate itself
``to_domain`` / ``from_domain``. Each row class states its domain→column mapping **once**, in
``column_values``, which both the insert (``from_domain``) and the update (``_apply_columns``)
go through — so a newly added column can't be carried on create and silently dropped on update.
Parent→child links are ORM ``relationship``\\ s (loaded
eagerly via ``selectin``), so reading a task pulls in its history and responsibilities and
writing one cascades — no hand-written load/insert code.

The integrity checks are enforced by the base :class:`~panopticon.core.store.Store`
template methods; this adapter implements the persistence primitives. ``_update_task`` only
appends new entries and updates the current entry's promises in place — never rewriting
recorded history — rather than letting the unit-of-work write whatever is dirty.

Schema management: call :meth:`SqlAlchemyStore.init` (async) to bootstrap the schema before
first use. All writes go through the async engine (``aiosqlite`` for SQLite URLs). Alembic
owns versioned evolution of any persistent DB (``migrations/``, ADR 0001 §3) — its
``alembic.ini`` keeps a plain ``sqlite://`` URL so the Alembic tooling stays synchronous; the
adapter translates ``sqlite://`` → ``sqlite+aiosqlite://`` internally. The initial migration
reproduces this exact schema, and ``tests/test_migrations.py`` guards the two against drift.
On a persistent deployment, run ``alembic upgrade head`` to apply migrations; ``alembic stamp
head`` aligns a dev database that ``create_all`` already bootstrapped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import JSON, ForeignKey, ForeignKeyConstraint, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    noload,
    relationship,
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


def _to_async_url(url: str) -> str:
    """Translate a plain ``sqlite://`` URL to its ``sqlite+aiosqlite://`` equivalent."""
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite" + url[len("sqlite") :]
    return url


# -- ORM row classes (mutable; live only in this adapter; own their translation) ----


class _Base(DeclarativeBase):
    pass


#: The schema's table metadata — Alembic's autogenerate target (``migrations/env.py``) and the
#: single source of truth migrations are checked against (``tests/test_migrations.py``).
metadata = _Base.metadata


def _apply_columns(row: _Base, values: dict[str, Any]) -> None:
    """Overwrite ``row``'s columns from a ``column_values`` mapping, leaving its ``id`` alone.

    The counterpart to each row class's ``from_domain``: an update writes exactly the columns an
    insert does, so a newly added column can't be persisted on create and then silently dropped
    on update (which is how ``Repo.agent_cli`` was lost — see ``column_values``).
    """
    for key, value in values.items():
        if key != "id":
            setattr(row, key, value)


class _RepoRow(_Base):
    __tablename__ = "repo"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    git_url: Mapped[str]
    default_base: Mapped[str]
    env_file: Mapped[str | None] = mapped_column(default=None)
    credential_dir: Mapped[str | None] = mapped_column(default=None)
    image_layer_file: Mapped[str | None] = mapped_column(default=None)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hook_file: Mapped[str | None] = mapped_column(default=None)
    enabled_workflows: Mapped[list[str]] = mapped_column(JSON, default=list)
    disabled_workflows: Mapped[list[str]] = mapped_column(JSON, default=list)
    agent_cli: Mapped[str] = mapped_column(default="claude", server_default="claude")

    def to_domain(self) -> Repo:
        return Repo(
            id=self.id,
            name=self.name,
            git_url=self.git_url,
            default_base=self.default_base,
            env_file=self.env_file,
            credential_dir=self.credential_dir,
            image_layer_file=self.image_layer_file,
            capabilities=dict(self.capabilities or {}),
            hook_file=self.hook_file,
            agent_cli=self.agent_cli,
            enabled_workflows=list(self.enabled_workflows or []),
            disabled_workflows=list(self.disabled_workflows or []),
        )

    @classmethod
    def column_values(cls, repo: Repo) -> dict[str, Any]:
        """The domain→row column mapping: the one place a new repo column gets wired up.

        Both the insert (:meth:`from_domain`) and the update (:func:`_apply_columns`) read it, so
        the two can't drift — the failure mode this replaces was ``agent_cli`` being carried on
        create and dropped by a hand-copied update, making a repo un-switchable to another CLI.
        """
        return {
            "id": repo.id,
            "name": repo.name,
            "git_url": repo.git_url,
            "default_base": repo.default_base,
            "env_file": repo.env_file,
            "credential_dir": repo.credential_dir,
            "image_layer_file": repo.image_layer_file,
            "capabilities": dict(repo.capabilities),
            "hook_file": repo.hook_file,
            "agent_cli": repo.agent_cli,
            "enabled_workflows": list(repo.enabled_workflows),
            "disabled_workflows": list(repo.disabled_workflows),
        }

    @classmethod
    def from_domain(cls, repo: Repo) -> _RepoRow:
        return cls(**cls.column_values(repo))


class _TaskRow(_Base):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(primary_key=True)
    repo_id: Mapped[str] = mapped_column(ForeignKey("repo.id"))
    workflow: Mapped[str]
    state: Mapped[str]
    turn: Mapped[str]
    blocked: Mapped[bool] = mapped_column(default=False)
    memo: Mapped[str | None] = mapped_column(default=None)
    initial_prompt: Mapped[str | None] = mapped_column(default=None)
    slug: Mapped[str | None]
    url: Mapped[str | None] = mapped_column(default=None)
    snoozed_until: Mapped[str | None] = mapped_column(default=None)
    branch: Mapped[str | None] = mapped_column(default=None)
    clone: Mapped[str | None] = mapped_column(default=None)
    claimed_by: Mapped[str | None] = mapped_column(default=None)
    starting_model: Mapped[str | None] = mapped_column(default=None)
    agent_cli: Mapped[str | None] = mapped_column(default=None)
    governor_task_id: Mapped[str | None] = mapped_column(ForeignKey("task.id"), default=None)
    created_at: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[str | None] = mapped_column(default=None)
    sort_weight: Mapped[int] = mapped_column(default=0, server_default="0")
    depends_on_task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
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
            memo=self.memo,
            initial_prompt=self.initial_prompt,
            slug=self.slug,
            url=self.url,
            snoozed_until=self.snoozed_until,
            branch=self.branch,
            clone=self.clone,
            claimed_by=self.claimed_by,
            starting_model=self.starting_model,
            agent_cli=self.agent_cli,
            governor_task_id=self.governor_task_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            sort_weight=self.sort_weight,
            depends_on_task_ids=list(self.depends_on_task_ids or []),
            history=[h.to_domain() for h in self.history],
        )

    @classmethod
    def column_values(cls, task: Task) -> dict[str, Any]:
        """The domain→row column mapping: the one place a new task column gets wired up.

        Columns only — ``history`` is a relationship, persisted append-only by ``_update_task``
        (see the module docstring), never through this.
        """
        return {
            "id": task.id,
            "repo_id": task.repo_id,
            "workflow": task.workflow,
            "state": task.state,
            "turn": task.turn.value,
            "blocked": task.blocked,
            "memo": task.memo,
            "initial_prompt": task.initial_prompt,
            "slug": task.slug,
            "url": task.url,
            "snoozed_until": task.snoozed_until,
            "branch": task.branch,
            "clone": task.clone,
            "claimed_by": task.claimed_by,
            "starting_model": task.starting_model,
            "agent_cli": task.agent_cli,
            "governor_task_id": task.governor_task_id,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "sort_weight": task.sort_weight,
            "depends_on_task_ids": list(task.depends_on_task_ids),
        }

    @classmethod
    def from_domain(cls, task: Task) -> _TaskRow:
        return cls(
            **cls.column_values(task),
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
    __table_args__ = (ForeignKeyConstraint(["task_id", "seq"], ["history.task_id", "history.seq"]),)

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
            key=self.key,
            description=self.description,
            status=Status(self.status),
            comment=self.comment,
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
    for row, r in zip(existing, entry.responsibilities, strict=False):
        row.status = r.status.value
        row.comment = r.comment


class SqlAlchemyStore(Store):
    """A :class:`~panopticon.core.store.Store` backed by async SQLAlchemy (aiosqlite for SQLite).

    Call :meth:`init` before first use to create the schema (or rely on the task service's
    lifespan hook, which does it automatically).
    """

    def __init__(self, url: str = "sqlite://") -> None:
        super().__init__()  # the base Store's change-feed counter + listeners
        async_url = _to_async_url(url)
        if url in _IN_MEMORY:
            # Pin one connection for in-memory SQLite so schema and data survive across sessions.
            self._engine = create_async_engine(
                async_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        else:
            self._engine = create_async_engine(async_url)
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema (idempotent). Must be called before first use."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # -- repos --------------------------------------------------------------------

    async def _create_repo(self, repo: Repo) -> None:
        async with self._session.begin() as s:
            if await s.get(_RepoRow, repo.id) is not None:
                raise AlreadyExists(f"repo {repo.id!r} already exists")
            s.add(_RepoRow.from_domain(repo))

    async def _get_repo(self, repo_id: str) -> Repo | None:
        async with self._session() as s:
            row = await s.get(_RepoRow, repo_id)
            return row.to_domain() if row is not None else None

    async def _list_repos(self) -> list[Repo]:
        async with self._session() as s:
            result = await s.execute(select(_RepoRow).order_by(_RepoRow.id))
            return [r.to_domain() for r in result.scalars()]

    async def _update_repo(self, repo: Repo) -> None:
        async with self._session.begin() as s:
            row = await s.get(_RepoRow, repo.id)
            if row is None:
                raise NotFound(f"repo {repo.id!r} does not exist")
            _apply_columns(row, _RepoRow.column_values(repo))

    # -- tasks: reads + persistence primitives (the base's template methods drive these) --

    async def _get_task(self, task_id: str) -> Task | None:
        async with self._session() as s:
            row = await s.get(_TaskRow, task_id)
            return row.to_domain() if row is not None else None

    async def _list_tasks(self) -> list[Task]:
        async with self._session() as s:
            result = await s.execute(select(_TaskRow).order_by(_TaskRow.id))
            return [r.to_domain() for r in result.scalars()]

    async def _list_tasks_summary(self) -> list[Task]:
        async with self._session() as s:
            result = await s.execute(
                select(_TaskRow).options(noload(_TaskRow.history)).order_by(_TaskRow.id)
            )
            return [r.to_domain() for r in result.scalars()]

    async def _create_task(self, task: Task) -> None:
        async with self._session.begin() as s:
            if await s.get(_TaskRow, task.id) is not None:
                raise AlreadyExists(f"task {task.id!r} already exists")
            if await s.get(_RepoRow, task.repo_id) is None:
                raise NotFound(f"repo {task.repo_id!r} does not exist")
            s.add(_TaskRow.from_domain(task))  # cascade inserts history + responsibilities

    async def _stored_history(self, task_id: str) -> list[HistoryEntry]:
        async with self._session() as s:
            row = await s.get(_TaskRow, task_id)
            if row is None:
                raise NotFound(f"task {task_id!r} does not exist")
            return [h.to_domain() for h in row.history]

    async def _update_task(self, task: Task, stored: Sequence[HistoryEntry]) -> None:
        async with self._session.begin() as s:
            row = await s.get(_TaskRow, task.id)
            if row is None:  # defensive: single-writer, so it still exists after _stored_history
                raise NotFound(f"task {task.id!r} does not exist")
            _apply_columns(row, _TaskRow.column_values(task))
            # The current (last stored) entry's promises may have been fulfilled in place.
            if stored:
                _fulfil_current_promises(
                    row.history[len(stored) - 1], task.history[len(stored) - 1]
                )
            # Append any new entries; the relationship cascade inserts them and their children.
            for seq in range(len(stored), len(task.history)):
                row.history.append(_HistoryRow.from_domain(task.history[seq], seq))
