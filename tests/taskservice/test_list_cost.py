"""What keeps ``GET /tasks`` cheap under load: the terminal-task cap, the memoised artifact mark,
the batched container status, and the restatement writes that no longer wake the change feed.

A host that has run for weeks holds hundreds of finished tasks, and every long-poller (the host
daemon, the dashboard) re-reads the list on every change — so each of these is multiplied by the
list size *and* by the write rate.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

from panopticon.core.models import Actor, LifecyclePhase, Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


class _CountingArtifacts(FilesystemArtifactStore):
    """The filesystem store, counting how many task ids reach the batched presence scan."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.scanned: list[str] = []

    async def tasks_with_unhidden_artifacts(self, task_ids: Iterable[str]) -> set[str]:
        ids = list(task_ids)
        self.scanned.extend(ids)
        return await super().tasks_with_unhidden_artifacts(ids)


async def _service(tmp_path: Path) -> tuple[TaskService, _CountingArtifacts]:
    # A zero-padded clock so ``updated_at`` sorts the way time does (``t10`` would sort before
    # ``t9``) — the terminal cap keeps the *newest* trees.
    times: Iterator[str] = iter(f"t{i:05d}" for i in range(1, 100_000))
    artifacts = _CountingArtifacts(tmp_path)
    svc = TaskService(SqlAlchemyStore(), {"spike": Spike()}, artifacts, clock=lambda: next(times))
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc, artifacts


async def _finished(svc: TaskService, *, governor: str | None = None) -> str:
    task = await svc.create_task("r1", "spike", governor_task_id=governor)
    await svc.apply_operation(task.id, "drop")
    return task.id


# -- the terminal cap ------------------------------------------------------------------------


async def test_terminal_limit_keeps_every_active_task_and_the_newest_finished(
    tmp_path: Path,
) -> None:
    svc, _ = await _service(tmp_path)
    old, mid, new = [await _finished(svc) for _ in range(3)]
    active = (await svc.create_task("r1", "spike")).id

    ids = {t.id for t in await svc.list_tasks_summary(terminal_limit=2)}
    assert ids == {active, mid, new}  # the oldest finished task is the one capped away
    assert old not in ids
    assert len(await svc.list_tasks_summary()) == 4  # uncapped by default


async def test_terminal_limit_never_orphans_a_child_of_an_active_governor(tmp_path: Path) -> None:
    # The dashboard files a finished child of an active governor in the *active* section, under
    # it — however old the child is, the cap must not take it away.
    svc, _ = await _service(tmp_path)
    governor = (await svc.create_task("r1", "spike")).id
    stale_child = await _finished(svc, governor=governor)
    await _finished(svc)  # a newer, unrelated finished task fills the cap

    ids = {t.id for t in await svc.list_tasks_summary(terminal_limit=1)}
    assert stale_child in ids


async def test_terminal_limit_counts_finished_trees_not_rows(tmp_path: Path) -> None:
    # A kept finished governor brings its whole finished ensemble with it, and the ensemble counts
    # once against the cap — a capped list never shows a tree with members missing.
    svc, _ = await _service(tmp_path)
    loner = await _finished(svc)
    governor = (await svc.create_task("r1", "spike")).id
    children = [await _finished(svc, governor=governor) for _ in range(3)]
    await svc.apply_operation(governor, "drop")  # the newest root

    ids = {t.id for t in await svc.list_tasks_summary(terminal_limit=1)}
    assert ids == {governor, *children}
    assert loner not in ids


async def test_terminal_limit_composes_with_the_terminal_filter(tmp_path: Path) -> None:
    svc, _ = await _service(tmp_path)
    governor = (await svc.create_task("r1", "spike")).id
    child = await _finished(svc, governor=governor)
    newest = await _finished(svc)

    # The cap reads governors' states before the filter drops them, so the child of an active
    # governor still survives a terminal-only, capped read.
    ids = {t.id for t in await svc.list_tasks_summary(terminal=True, terminal_limit=1)}
    assert ids == {child, newest}


async def test_api_takes_a_terminal_limit(tmp_path: Path) -> None:
    svc, _ = await _service(tmp_path)
    await _finished(svc)
    newest = await _finished(svc)
    active = (await svc.create_task("r1", "spike")).id
    transport = httpx.ASGITransport(app=create_app(svc))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        capped = await http.get("/tasks", params={"terminal_limit": 1})
        assert {t["id"] for t in capped.json()} == {active, newest}
        assert len((await http.get("/tasks")).json()) == 3
        assert (await http.get("/tasks", params={"terminal_limit": -1})).status_code == 422


# -- the artifact mark ---------------------------------------------------------------------


async def test_artifact_marks_are_scanned_in_one_batch_and_then_remembered(tmp_path: Path) -> None:
    svc, artifacts = await _service(tmp_path)
    with_plan = await _finished(svc)
    bare = await _finished(svc)
    (tmp_path / "tasks" / with_plan).mkdir(parents=True)
    (tmp_path / "tasks" / with_plan / "plan.md").write_text("# Plan")
    tasks = await svc.list_tasks_summary()

    assert await svc.tasks_with_unhidden_artifacts(tasks) == {with_plan}
    assert sorted(artifacts.scanned) == sorted([with_plan, bare])  # one batched scan of both

    artifacts.scanned.clear()
    assert await svc.tasks_with_unhidden_artifacts(tasks) == {with_plan}
    assert artifacts.scanned == []  # finished tasks: both answers are remembered


async def test_an_active_task_without_artifacts_is_re_probed(tmp_path: Path) -> None:
    # An active task's agent may write an artifact at any moment, and an operator may drop one into
    # its folder by hand (outside the service) — so its "no" isn't remembered, only its "yes".
    svc, artifacts = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    assert await svc.tasks_with_unhidden_artifacts([task]) == set()

    (tmp_path / "tasks" / task.id).mkdir(parents=True)
    (tmp_path / "tasks" / task.id / "notes.md").write_text("by hand")
    assert await svc.tasks_with_unhidden_artifacts([task]) == {task.id}

    artifacts.scanned.clear()
    assert await svc.tasks_with_unhidden_artifacts([task]) == {task.id}
    assert artifacts.scanned == []  # a "yes" is remembered (artifacts are never deleted)


async def test_put_artifact_updates_the_remembered_mark(tmp_path: Path) -> None:
    svc, _ = await _service(tmp_path)
    task_id = await _finished(svc)
    assert not await svc.has_unhidden_artifacts(task_id)  # remembered as "no" (finished)

    await svc.put_artifact(task_id, ".babysit-ci-state.json", b"{}")
    assert not await svc.has_unhidden_artifacts(task_id)  # a hidden artifact doesn't count
    await svc.put_artifact(task_id, "plan.md", b"# Plan")
    assert await svc.has_unhidden_artifacts(task_id)


# -- the batched container status ----------------------------------------------------------


async def test_container_statuses_match_the_per_task_answer(tmp_path: Path) -> None:
    svc, _ = await _service(tmp_path)
    await svc.create_task("r1", "spike")  # unclaimed → queued
    live = await svc.create_task("r1", "spike")
    building = await svc.create_task("r1", "spike")
    lost = await svc.create_task("r1", "spike")
    finished = await svc.create_task("r1", "spike")
    await svc.apply_operation(finished.id, "drop")
    await svc.register_runner("host-1")
    for task in (live, building):
        await svc.claim(task.id, "host-1")
    await svc.claim(lost.id, "host-gone")  # a runner that isn't connected
    await svc.register(live.id, "c1", "host-1")
    await svc.report_lifecycle(building.id, "host-1", LifecyclePhase.BUILDING)

    tasks = await svc.list_tasks_summary()
    batch = svc.container_statuses(tasks)
    assert batch == {t.id: svc.container_status(t) for t in tasks}
    assert len(set(batch.values())) == 5  # queued/live/building/disconnected/–: all distinct


# -- restatements don't wake the feed ------------------------------------------------------


async def test_restating_the_turn_or_blocked_neither_saves_nor_bumps(tmp_path: Path) -> None:
    # The turn hooks fire on every prompt and every stop, so most calls restate the current turn;
    # each used to bump the change feed and make every long-poller re-read the whole list.
    svc, _ = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.set_turn(task.id, Actor.AGENT)
    stamped = (await svc.get_task(task.id)).updated_at
    version = svc.tasks_version()

    await svc.set_turn(task.id, Actor.AGENT)
    await svc.set_blocked(task.id, False)
    assert svc.tasks_version() == version
    assert (await svc.get_task(task.id)).updated_at == stamped  # no save at all

    await svc.set_turn(task.id, Actor.USER)  # a real flip still saves and wakes the feed
    assert svc.tasks_version() > version
    version = svc.tasks_version()
    await svc.set_blocked(task.id, True)
    assert svc.tasks_version() > version


async def test_re_reporting_the_same_lifecycle_phase_does_not_bump(tmp_path: Path) -> None:
    svc, _ = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.FAILED, "exit 1")
    version = svc.tasks_version()

    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.FAILED, "exit 1")
    assert svc.tasks_version() == version
    lifecycle = svc.lifecycle(task.id)
    assert lifecycle is not None and lifecycle.detail == "exit 1"  # still recorded (fresher ``at``)

    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.FAILED, "OOM killed")
    assert svc.tasks_version() > version  # a new detail is a change the dashboard shows
