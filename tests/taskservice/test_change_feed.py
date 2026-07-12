"""The block-until-change feed on ``GET /tasks`` — the mechanism three polling loops (the host
daemon, the provision daemon, the dashboard) will migrate onto instead of interval re-polling.

Driven over the in-memory app the way ``test_skeleton`` / ``test_mcp`` do (no Docker, no LLM):
an :class:`httpx.AsyncClient` over an ASGI transport so a parked long-poll and a concurrent
mutation run on one event loop. Asserts the request actually *blocks* until a change, that a
stale cursor returns immediately, and that a quiet wait times out without error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from panopticon.core.models import Repo
from panopticon.taskservice.api import TASKS_VERSION_HEADER, create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


async def _service(tmp_path: Path) -> TaskService:
    svc = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


def _client(svc: TaskService) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(svc))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_plain_list_carries_the_version_header(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    async with _client(svc) as http:
        before = await http.get("/tasks")
        assert before.headers[TASKS_VERSION_HEADER] == "0"  # no task written yet
        await svc.create_task("r1", "spike")
        after = await http.get("/tasks")
        assert int(after.headers[TASKS_VERSION_HEADER]) > 0


async def test_long_poll_blocks_until_a_concurrent_mutation(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.create_task("r1", "spike")
    async with _client(svc) as http:
        snapshot = await http.get("/tasks")
        version = int(snapshot.headers[TASKS_VERSION_HEADER])

        # Park a long-poll on the current version: nothing has changed, so it must block.
        waiter = asyncio.ensure_future(http.get("/tasks", params={"wait": 5, "since": version}))
        await asyncio.sleep(0.05)  # give the request time to reach the waiter
        assert not waiter.done()  # still blocked — the feed is event-driven, not a busy return

        # A concurrent task creation wakes it with the fresh snapshot + bumped version.
        created = await http.post(
            "/tasks", json={"repo_id": "r1", "workflow": "spike", "description": None}
        )
        assert created.status_code == 201

        resp = await asyncio.wait_for(waiter, timeout=1)
        assert int(resp.headers[TASKS_VERSION_HEADER]) > version
        assert len(resp.json()) == 2  # the new task is visible in the woken snapshot


async def test_stale_cursor_returns_immediately(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    async with _client(svc) as http:
        version = int((await http.get("/tasks")).headers[TASKS_VERSION_HEADER])
        await svc.create_task("r1", "spike")  # version moves past the cursor before we ask

        # Even with a long ?wait, a stale ?since returns at once (no blocking).
        resp = await asyncio.wait_for(
            http.get("/tasks", params={"wait": 30, "since": version}), timeout=1
        )
        assert int(resp.headers[TASKS_VERSION_HEADER]) > version
        assert len(resp.json()) == 1


async def test_quiet_wait_times_out_without_changing_the_version(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.create_task("r1", "spike")
    async with _client(svc) as http:
        version = int((await http.get("/tasks")).headers[TASKS_VERSION_HEADER])
        # No mutation: the wait elapses and returns the same version (a 200, not an error).
        resp = await http.get("/tasks", params={"wait": 0.1, "since": version})
        assert resp.status_code == 200
        assert int(resp.headers[TASKS_VERSION_HEADER]) == version
