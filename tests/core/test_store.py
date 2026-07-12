"""Store contract tests — run against the SQLAlchemy adapter.

Parametrizing over in-memory and on-disk SQLite engines exercises the same code path against
a real connection, and proves the integrity rules hold. The "domain / persistence sync"
section additionally guards (by reflection) that the ORM rows and the domain dataclasses can't
silently drift apart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

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
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    # Both backends are the one SQLAlchemy adapter; "memory" is an in-memory SQLite engine,
    # "file" is on-disk SQLite (so we also exercise persistence across a real connection).
    if request.param == "memory":
        r = SqlAlchemyStore("sqlite://")
    else:
        r = SqlAlchemyStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    await r.init()
    yield r
    await r.close()


async def _seed_repo(store: Store, repo_id: str = "r1") -> None:
    await store.create_repo(
        Repo(id=repo_id, name="acme/widgets", git_url="https://github.com/acme/widgets.git")
    )


async def _new_task(store: Store, task_id: str = "t1", repo_id: str = "r1") -> Task:
    task = WF.start_task(task_id, repo_id, at="t0")
    await store.create_task(task)
    return task


# -- repos --------------------------------------------------------------------------


async def test_create_and_get_repo(store: Store) -> None:
    await _seed_repo(store)
    got = await store.get_repo("r1")
    assert got is not None
    assert got.name == "acme/widgets"
    assert got.default_base == "main"
    assert got.env_file is None  # the env-file reference defaults to unset
    assert got.image_layer_file is None  # no repo image layer by default
    assert got.capabilities == {}  # no elevated capabilities by default


async def test_repo_secret_references_round_trip(store: Store) -> None:
    await store.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            env_file="r1.env",
            image_layer_file="r1.layer",
            capabilities={"docker_in_docker": True},
        )
    )
    got = await store.get_repo("r1")
    assert got is not None
    assert got.env_file == "r1.env"
    assert got.capabilities == {"docker_in_docker": True}  # JSON capabilities round-trip
    assert got.image_layer_file == "r1.layer"  # ADR 0005 repo tier round-trips


async def test_get_missing_repo_returns_none(store: Store) -> None:
    assert await store.get_repo("nope") is None


async def test_duplicate_repo_raises(store: Store) -> None:
    await _seed_repo(store)
    with pytest.raises(AlreadyExists):
        await _seed_repo(store)


async def test_list_repos(store: Store) -> None:
    await store.create_repo(Repo(id="r1", name="a", git_url="https://x/a.git"))
    await store.create_repo(Repo(id="r2", name="b", git_url="https://x/b.git"))
    assert {r.id for r in await store.list_repos()} == {"r1", "r2"}


async def test_update_repo_round_trips(store: Store) -> None:
    await store.create_repo(
        Repo(
            id="r1",
            name="old",
            git_url="https://x/old.git",
            image_layer_file="r1.layer",
            capabilities={"docker_in_docker": True},
        )
    )
    await store.update_repo(
        Repo(
            id="r1",
            name="new",
            git_url="https://x/new.git",
            default_base="trunk",
            env_file="r1.env",
            image_layer_file="r1.layer",
            capabilities={"docker_in_docker": True},
        )
    )
    got = await store.get_repo("r1")
    assert got is not None
    assert got.name == "new"
    assert got.git_url == "https://x/new.git"
    assert got.default_base == "trunk"
    assert got.env_file == "r1.env"
    assert got.image_layer_file == "r1.layer"  # untouched fields persist
    assert got.capabilities == {"docker_in_docker": True}


async def test_update_missing_repo_raises(store: Store) -> None:
    with pytest.raises(NotFound):
        await store.update_repo(Repo(id="ghost", name="x", git_url="https://x/x.git"))


# -- task creation ------------------------------------------------------------------


async def test_create_task_requires_existing_repo(store: Store) -> None:
    task = WF.start_task("t1", "ghost", at="t0")
    with pytest.raises(NotFound):
        await store.create_task(task)


async def test_create_and_get_task_roundtrips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    task.slug = "fix-widget"  # not yet persisted
    got = await store.get_task("t1")
    assert got is not None
    assert got.state == "ITERATING"
    assert got.turn is Actor.USER  # spike's initial state → turn starts with the user
    assert got.workflow == "spike"
    assert got.slug is None  # create persisted before slug was set
    assert [(h.from_state, h.to_state) for h in got.history] == [(None, "ITERATING")]


async def test_duplicate_task_raises(store: Store) -> None:
    await _seed_repo(store)
    await _new_task(store)
    with pytest.raises(AlreadyExists):
        await _new_task(store)


async def test_create_task_rejects_inconsistent_state(store: Store) -> None:
    await _seed_repo(store)
    bad = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="COMPLETE",  # disagrees with history tail
        turn=Actor.AGENT,
        history=[HistoryEntry(at="t0", from_state=None, to_state="ITERATING")],
    )
    with pytest.raises(IntegrityError):
        await store.create_task(bad)


# -- saving / append-only -----------------------------------------------------------


async def test_save_persists_transition_and_slug(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    WF.apply_transition(task, "COMPLETE", at="t1", trigger="finish")
    task.slug = "fix-widget"
    await store.save_task(task)

    got = await store.get_task("t1")
    assert got is not None
    assert got.state == "COMPLETE"
    assert got.turn is Actor.USER  # COMPLETE is a terminal (foreground) state
    assert got.slug == "fix-widget"
    assert [h.to_state for h in got.history] == ["ITERATING", "COMPLETE"]


async def test_url_round_trips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    assert (await store.get_task("t1")).url is None  # type: ignore[union-attr]
    task.url = "https://github.com/acme/widgets/pull/7"
    await store.save_task(task)
    assert (await store.get_task("t1")).url == "https://github.com/acme/widgets/pull/7"  # type: ignore[union-attr]


async def test_tokens_used_round_trips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    assert (await store.get_task("t1")).tokens_used is None  # type: ignore[union-attr]
    task.tokens_used = 12750
    await store.save_task(task)
    assert (await store.get_task("t1")).tokens_used == 12750  # type: ignore[union-attr]


async def test_token_estimate_round_trips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    assert (await store.get_task("t1")).token_estimate is None  # type: ignore[union-attr]
    task.token_estimate = 500_000
    await store.save_task(task)
    assert (await store.get_task("t1")).token_estimate == 500_000  # type: ignore[union-attr]


async def test_blocked_marker_round_trips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    assert (await store.get_task("t1")).blocked is False  # type: ignore[union-attr]
    task.blocked = True
    await store.save_task(task)
    assert (await store.get_task("t1")).blocked is True  # type: ignore[union-attr]


async def test_depends_on_defaults_to_empty(store: Store) -> None:
    await _seed_repo(store)
    await _new_task(store)
    got = await store.get_task("t1")
    assert got is not None
    assert got.depends_on_task_ids == []


async def test_depends_on_round_trips(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    task.depends_on_task_ids = ["t-a", "t-b"]
    await store.save_task(task)
    got = await store.get_task("t1")
    assert got is not None
    assert got.depends_on_task_ids == ["t-a", "t-b"]


async def test_depends_on_replace_clears_previous(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    task.depends_on_task_ids = ["t-a", "t-b"]
    await store.save_task(task)
    task2 = await store.get_task("t1")
    assert task2 is not None
    task2.depends_on_task_ids = ["t-c"]
    await store.save_task(task2)
    got = await store.get_task("t1")
    assert got is not None
    assert got.depends_on_task_ids == ["t-c"]  # previous deps replaced, not merged


async def test_depends_on_cleared_to_empty(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    task.depends_on_task_ids = ["t-a"]
    await store.save_task(task)
    task2 = await store.get_task("t1")
    assert task2 is not None
    task2.depends_on_task_ids = []
    await store.save_task(task2)
    got = await store.get_task("t1")
    assert got is not None
    assert got.depends_on_task_ids == []


async def test_save_missing_task_raises(store: Store) -> None:
    await _seed_repo(store)
    task = WF.start_task("ghost", "r1", at="t0")
    with pytest.raises(NotFound):
        await store.save_task(task)


async def test_save_rejects_history_rewrite(store: Store) -> None:
    await _seed_repo(store)
    await _new_task(store)
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
        await store.save_task(tampered)


async def test_save_rejects_history_shrink(store: Store) -> None:
    await _seed_repo(store)
    task = await _new_task(store)
    WF.apply_transition(task, "COMPLETE", at="t1")
    await store.save_task(task)  # stored history now length 2

    truncated = Task(
        id="t1",
        repo_id="r1",
        workflow="spike",
        state="ITERATING",
        turn=Actor.AGENT,
        history=[HistoryEntry(at="t0", from_state=None, to_state="ITERATING")],
    )
    with pytest.raises(IntegrityError):
        await store.save_task(truncated)


# -- change feed (block-until-change cursor) ----------------------------------------


async def test_version_starts_at_zero_and_bumps_on_each_task_write(store: Store) -> None:
    await _seed_repo(store)  # repo writes are not task mutations
    assert store.version() == 0

    task = await _new_task(store)  # create_task
    v_after_create = store.version()
    assert v_after_create > 0

    WF.apply_transition(task, "COMPLETE", at="t1", trigger="finish")
    await store.save_task(task)  # save_task
    assert store.version() > v_after_create


async def test_subscribed_listener_fires_on_every_task_mutation(store: Store) -> None:
    bumps: list[int] = []
    store.subscribe(lambda: bumps.append(store.version()))

    await _seed_repo(store)  # a repo write must not wake task-change subscribers
    assert bumps == []

    task = await _new_task(store)
    task.slug = "name-it"
    await store.save_task(task)

    # One notification per task write (create + save), each carrying the bumped version.
    assert bumps == [1, 2]


# -- isolation ----------------------------------------------------------------------


async def test_get_returns_independent_copy(store: Store) -> None:
    await _seed_repo(store)
    await _new_task(store)
    got = await store.get_task("t1")
    assert got is not None
    got.state = "COMPLETE"  # mutating the returned object must not change storage
    again = await store.get_task("t1")
    assert again is not None
    assert again.state == "ITERATING"


async def test_resolved_responsibilities_roundtrip(store: Store) -> None:
    await _seed_repo(store)
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
    await store.create_task(task)

    got = await store.get_task("t1")
    assert got is not None
    assert got.history[0].responsibilities == resolved  # order, status, and comment preserved
    assert got.history[1].responsibilities == []


async def test_current_entry_responsibilities_persist_in_place(store: Store) -> None:
    await _seed_repo(store)
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
    await store.create_task(task)

    # Fulfil the promise on the current entry and save — the in-place change must persist.
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    await store.save_task(task)

    got = await store.get_task("t1")
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
    """Domain field names that should map to a column.

    Excludes entity-list fields (e.g. ``list[HistoryEntry]``) that are ORM relationships,
    but includes primitive-list fields (e.g. ``list[str]``) that are stored as JSON columns.
    """
    hints = get_type_hints(domain)

    def _is_column_backed(hint: Any) -> bool:
        if get_origin(hint) is not list:
            return True
        args = get_args(hint)
        return bool(args) and not is_dataclass(args[0])

    return {f.name for f in fields(domain) if _is_column_backed(hints[f.name])}


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
        memo="make the widget green",
        initial_prompt="review your plan",
        slug="fix-the-widget",
        url="https://github.com/acme/widgets/pull/7",
        branch="panopticon/fix-the-widget",
        clone="/clones/t-full",
        claimed_by="local",
        tokens_used=87500,
        token_estimate=500_000,
        starting_model="opus",
        governor_task_id="t-governor",
        created_at="t1",
        updated_at="t2",
        depends_on_task_ids=["t-dep"],
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
                        key="tests-pass",
                        description="Tests pass",
                        status=Status.MET,
                        comment="green",
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
            pytest.fail(
                f"{domain.__name__}.{f.name} is never exercised — extend _fully_populated_task"
            )


async def test_full_task_round_trips_and_exercises_every_field(store: Store) -> None:
    await _seed_repo(store)
    task = _fully_populated_task()
    entries = task.history
    responsibilities = [r for e in entries for r in e.responsibilities]
    _assert_every_field_exercised([task], Task)
    _assert_every_field_exercised(entries, HistoryEntry)
    _assert_every_field_exercised(responsibilities, Responsibility)

    await store.create_task(task)
    assert (
        await store.get_task(task.id) == task
    )  # every field survives the round trip (dataclass __eq__)


async def test_list_tasks_summary_returns_tasks_without_history(store: Store) -> None:
    await _seed_repo(store)
    await _new_task(store)
    tasks = await store.list_tasks_summary()
    assert len(tasks) == 1
    assert tasks[0].history == []  # no history loaded
    assert tasks[0].state == "ITERATING"  # scalar fields are present
    assert tasks[0].id == "t1"

    # list_tasks still returns full history
    full = await store.list_tasks()
    assert len(full[0].history) == 1
