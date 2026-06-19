"""Store contract tests — run against the SQLAlchemy adapter.

Parametrizing over in-memory and on-disk SQLite engines exercises the same code path against
a real connection, and proves the integrity rules hold. The "domain / persistence sync"
section additionally guards (by reflection) that the ORM rows and the domain dataclasses can't
silently drift apart.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any, get_origin, get_type_hints

import pytest
from sqlalchemy import inspect

from panopticon.core.models import Actor, HistoryEntry, Repo, Responsibility, Status, Task
from panopticon.core.store import (
    AlreadyExists,
    IntegrityError,
    NotFound,
    Store,
)
from panopticon.taskservice import store_sqlalchemy as rs
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

WF = Spike()


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    # Both backends are the one SQLAlchemy adapter; "memory" is an in-memory SQLite engine,
    # "file" is on-disk SQLite (so we also exercise persistence across a real connection).
    if request.param == "memory":
        r = SqlAlchemyStore("sqlite://")
    else:
        r = SqlAlchemyStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    yield r
    r.close()


def _seed_repo(store: Store, repo_id: str = "r1") -> None:
    store.create_repo(Repo(id=repo_id, name="acme/widgets", git_url="https://github.com/acme/widgets.git"))


def _new_task(store: Store, task_id: str = "t1", repo_id: str = "r1") -> Task:
    task = WF.start_task(task_id, repo_id, at="t0")
    store.create_task(task)
    return task


# -- repos --------------------------------------------------------------------------


def test_create_and_get_repo(store: Store) -> None:
    _seed_repo(store)
    got = store.get_repo("r1")
    assert got is not None
    assert got.name == "acme/widgets"
    assert got.default_base == "main"
    assert got.env_file is None and got.creds_volume is None  # references default to unset
    assert got.image_layer is None  # no repo image layer by default
    assert got.capabilities == {}  # no elevated capabilities by default


def test_repo_secret_references_round_trip(store: Store) -> None:
    store.create_repo(
        Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git",
             env_file="/secrets/r1.env", creds_volume="panopticon-creds-r1",
             image_layer="RUN pip install uv", capabilities={"docker_in_docker": True})
    )
    got = store.get_repo("r1")
    assert got is not None
    assert got.env_file == "/secrets/r1.env"
    assert got.creds_volume == "panopticon-creds-r1"
    assert got.image_layer == "RUN pip install uv"
    assert got.capabilities == {"docker_in_docker": True}  # JSON capabilities round-trip
    assert got.image_layer == "RUN pip install uv"  # ADR 0005 repo tier round-trips


def test_get_missing_repo_returns_none(store: Store) -> None:
    assert store.get_repo("nope") is None


def test_duplicate_repo_raises(store: Store) -> None:
    _seed_repo(store)
    with pytest.raises(AlreadyExists):
        _seed_repo(store)


def test_list_repos(store: Store) -> None:
    store.create_repo(Repo(id="r1", name="a", git_url="https://x/a.git"))
    store.create_repo(Repo(id="r2", name="b", git_url="https://x/b.git"))
    assert {r.id for r in store.list_repos()} == {"r1", "r2"}


# -- task creation ------------------------------------------------------------------


def test_create_task_requires_existing_repo(store: Store) -> None:
    task = WF.start_task("t1", "ghost", at="t0")
    with pytest.raises(NotFound):
        store.create_task(task)


def test_create_and_get_task_roundtrips(store: Store) -> None:
    _seed_repo(store)
    task = _new_task(store)
    task.slug = "fix-widget"  # not yet persisted
    got = store.get_task("t1")
    assert got is not None
    assert got.state == "ITERATING"
    assert got.turn is Actor.AGENT
    assert got.workflow == "spike"
    assert got.slug is None  # create persisted before slug was set
    assert [(h.from_state, h.to_state) for h in got.history] == [(None, "ITERATING")]


def test_duplicate_task_raises(store: Store) -> None:
    _seed_repo(store)
    _new_task(store)
    with pytest.raises(AlreadyExists):
        _new_task(store)


def test_create_task_rejects_inconsistent_state(store: Store) -> None:
    _seed_repo(store)
    bad = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="COMPLETE",  # disagrees with history tail
        turn=Actor.AGENT,
        history=[HistoryEntry(at="t0", from_state=None, to_state="ITERATING")],
    )
    with pytest.raises(IntegrityError):
        store.create_task(bad)


# -- saving / append-only -----------------------------------------------------------


def test_save_persists_transition_and_slug(store: Store) -> None:
    _seed_repo(store)
    task = _new_task(store)
    WF.apply_transition(task, "COMPLETE", at="t1", trigger="finish")
    task.slug = "fix-widget"
    store.save_task(task)

    got = store.get_task("t1")
    assert got is not None
    assert got.state == "COMPLETE"
    assert got.turn is Actor.USER  # COMPLETE is a terminal (foreground) state
    assert got.slug == "fix-widget"
    assert [h.to_state for h in got.history] == ["ITERATING", "COMPLETE"]


def test_blocked_marker_round_trips(store: Store) -> None:
    _seed_repo(store)
    task = _new_task(store)
    assert store.get_task("t1").blocked is False  # default on create  # type: ignore[union-attr]
    task.blocked = True
    store.save_task(task)
    assert store.get_task("t1").blocked is True  # type: ignore[union-attr]


def test_save_missing_task_raises(store: Store) -> None:
    _seed_repo(store)
    task = WF.start_task("ghost", "r1", at="t0")
    with pytest.raises(NotFound):
        store.save_task(task)


def test_save_rejects_history_rewrite(store: Store) -> None:
    _seed_repo(store)
    _new_task(store)
    # Same length as stored, but the (only) existing entry is altered.
    tampered = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="COMPLETE",
        turn=Actor.AGENT,
        history=[HistoryEntry(at="t0", from_state=None, to_state="COMPLETE")],
    )
    with pytest.raises(IntegrityError):
        store.save_task(tampered)


def test_save_rejects_history_shrink(store: Store) -> None:
    _seed_repo(store)
    task = _new_task(store)
    WF.apply_transition(task, "COMPLETE", at="t1")
    store.save_task(task)  # stored history now length 2

    truncated = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="ITERATING",
        turn=Actor.AGENT,
        history=[HistoryEntry(at="t0", from_state=None, to_state="ITERATING")],
    )
    with pytest.raises(IntegrityError):
        store.save_task(truncated)


# -- isolation ----------------------------------------------------------------------


def test_get_returns_independent_copy(store: Store) -> None:
    _seed_repo(store)
    _new_task(store)
    got = store.get_task("t1")
    assert got is not None
    got.state = "COMPLETE"  # mutating the returned object must not change storage
    again = store.get_task("t1")
    assert again is not None
    assert again.state == "ITERATING"


def test_resolved_responsibilities_roundtrip(store: Store) -> None:
    _seed_repo(store)
    # Responsibilities live on the entry for the state that defines them (WORKING here).
    resolved = [
        Responsibility(key="tests-pass", description="Tests pass", status=Status.MET),
        Responsibility(
            key="pr-opened", description="PR opened", status=Status.FAILED, comment="forge down"
        ),
    ]
    task = Task(
        id="t1",
        repo_id="r1",
        workflow="gated",
        state="COMPLETE",
        turn=Actor.AGENT,
        history=[
            HistoryEntry(at="t0", from_state=None, to_state="WORKING", responsibilities=resolved),
            HistoryEntry(at="t1", from_state="WORKING", to_state="COMPLETE"),
        ],
    )
    store.create_task(task)

    got = store.get_task("t1")
    assert got is not None
    assert got.history[0].responsibilities == resolved  # order, status, and comment preserved
    assert got.history[1].responsibilities == []


def test_current_entry_responsibilities_persist_in_place(store: Store) -> None:
    _seed_repo(store)
    task = Task(
        id="t1",
        repo_id="r1",
        workflow="gated",
        state="WORKING",
        turn=Actor.AGENT,
        history=[
            HistoryEntry(at="t0", from_state=None, to_state="PLAN"),
            HistoryEntry(
                at="t1",
                from_state="PLAN",
                to_state="WORKING",
                responsibilities=[Responsibility(key="tests-pass", description="Tests pass")],
            ),
        ],
    )
    store.create_task(task)

    # Fulfil the promise on the current entry and save — the in-place change must persist.
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    store.save_task(task)

    got = store.get_task("t1")
    assert got is not None
    assert got.history[-1].responsibilities[0].status is Status.MET


# -- domain / persistence sync ------------------------------------------------------
#
# Two reflective guards so the ORM rows and the domain dataclasses can't drift apart:
#  1. structural — every scalar domain field has a column (and no orphan columns);
#  2. behavioral — a fully-populated instance round-trips intact, and every field is
#     actually exercised (so a new field can't slip through unpersisted).

# domain class -> (row class, columns the table is allowed to add for persistence)
_SCHEMA: dict[type, tuple[type, set[str]]] = {
    Repo: (rs._RepoRow, set()),
    Responsibility: (rs._ResponsibilityRow, {"task_id", "seq", "idx"}),
    HistoryEntry: (rs._HistoryRow, {"task_id", "seq"}),
    Task: (rs._TaskRow, set()),
}


def _scalar_field_names(domain: type) -> set[str]:
    """Domain field names that should map to a column — i.e. excluding nested list fields."""
    hints = get_type_hints(domain)
    return {f.name for f in fields(domain) if get_origin(hints[f.name]) is not list}


@pytest.mark.parametrize("domain", list(_SCHEMA))
def test_rows_and_domain_models_stay_in_sync(domain: type) -> None:
    row, persistence_only = _SCHEMA[domain]
    scalar = _scalar_field_names(domain)
    columns = set(inspect(row).columns.keys())
    assert scalar <= columns, f"{domain.__name__}: domain fields with no column: {scalar - columns}"
    assert columns <= scalar | persistence_only, (
        f"{row.__name__}: columns not backed by a domain field: {columns - scalar - persistence_only}"
    )


def _fully_populated_task() -> Task:
    """A task touching every field of Task/HistoryEntry/Responsibility with a non-default value."""
    return Task(
        id="t-full",
        repo_id="r1",
        workflow="gated",
        state="WORKING",
        turn=Actor.AGENT,
        blocked=True,
        slug="fix-the-widget",
        branch="panopticon/fix-the-widget",
        clone="/clones/t-full",
        claimed_by="local",
        history=[
            HistoryEntry(
                at="t0", from_state=None, to_state="PLAN", trigger="start", note="kickoff"
            ),
            HistoryEntry(
                at="t1",
                from_state="PLAN",
                to_state="WORKING",
                trigger="advance",
                note="plan approved",
                responsibilities=[
                    Responsibility(
                        key="tests-pass", description="Tests pass", status=Status.MET, comment="green"
                    ),
                    Responsibility(
                        key="pr-opened",
                        description="PR opened",
                        status=Status.FAILED,
                        comment="forge down",
                    ),
                ],
            ),
        ],
    )


def _assert_every_field_exercised(instances: list[Any], domain: type) -> None:
    """Fail if any field of ``domain`` equals its default across *all* ``instances``.

    Forces the fixture to populate new fields with a real value, so the round-trip below
    genuinely tests them (a field left at its default would silently go unchecked).
    """
    for f in fields(domain):
        if f.default is not MISSING:
            default: object = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            default = f.default_factory()
        else:
            default = object()  # required field: any provided value differs from this sentinel
        if not any(getattr(i, f.name) != default for i in instances):
            pytest.fail(f"{domain.__name__}.{f.name} is never exercised — extend _fully_populated_task")


def test_full_task_round_trips_and_exercises_every_field(store: Store) -> None:
    _seed_repo(store)
    task = _fully_populated_task()
    entries = task.history
    responsibilities = [r for e in entries for r in e.responsibilities]
    _assert_every_field_exercised([task], Task)
    _assert_every_field_exercised(entries, HistoryEntry)
    _assert_every_field_exercised(responsibilities, Responsibility)

    store.create_task(task)
    assert store.get_task(task.id) == task  # every field survives the round trip (dataclass __eq__)
