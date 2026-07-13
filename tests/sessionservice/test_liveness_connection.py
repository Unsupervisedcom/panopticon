"""Liveness as a held connection: the ``/live`` stream registers on connect and reaps on disconnect.

Runs a *real* task service under uvicorn in a background thread and drives the client's held
``live`` connection over a real socket — so a genuine TCP disconnect (closing the stream) is what
removes the registration, the way a dying container does. No Docker, no LLM."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[TaskService, str]]:
    """A real TaskService served by uvicorn in a background thread; yields (service, base_url)."""
    import uvicorn

    service = TaskService(
        SqlAlchemyStore("sqlite://"), {"spike": Spike()}, FilesystemArtifactStore(tmp_path)
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    port = _free_port()
    config = uvicorn.Config(create_app(service), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: server.started, timeout=5), "uvicorn did not start"
        yield service, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_live_connection_registers_on_connect_and_reaps_on_disconnect(
    served: tuple[TaskService, str],
) -> None:
    service, base = served
    task_id = asyncio.run(service.create_task("r1", "spike")).id
    client = TaskServiceClient(httpx.Client(base_url=base, timeout=10.0))

    # Open the held liveness connection; the first tick means the server accepted it (registered).
    conn = client.live(task_id, container_id="c-live", runner_id="r-1")
    next(conn)
    assert _wait_until(lambda: bool(service.registrations(task_id))), "container never registered"
    assert [r.container_id for r in service.registrations(task_id)] == ["c-live"]

    # Dropping the connection (a dying container) reaps the registration immediately — no TTL.
    conn.close()
    assert _wait_until(lambda: not service.registrations(task_id)), (
        "registration not reaped on drop"
    )


def test_reconnect_re_registers_after_a_drop(served: tuple[TaskService, str]) -> None:
    service, base = served
    task_id = asyncio.run(service.create_task("r1", "spike")).id
    client = TaskServiceClient(httpx.Client(base_url=base, timeout=10.0))

    first = client.live(task_id, container_id="c-live")
    next(first)
    assert _wait_until(lambda: bool(service.registrations(task_id)))
    first.close()
    assert _wait_until(lambda: not service.registrations(task_id))

    # A transient blip self-heals: re-opening the connection produces a fresh live registration.
    second = client.live(task_id, container_id="c-live")
    next(second)
    assert _wait_until(lambda: bool(service.registrations(task_id))), (
        "did not re-register on reconnect"
    )
    second.close()
    assert _wait_until(lambda: not service.registrations(task_id))
