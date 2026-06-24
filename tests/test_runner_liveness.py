"""Host (runner) liveness as a held connection + reclaim — the container liveness model one layer up.

Runs a *real* task service under uvicorn in a background thread and drives the runner's held
``/runners/{id}/live`` connection over a real socket, so a genuine TCP disconnect (closing the
stream, or a dying daemon) is what drops the runner from ``live_runners`` — the way a dead host
does. ``reclaim`` is exercised over REST. No Docker, no LLM."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.sessionservice.host import hold_runner_liveness
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
    service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
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


def test_runner_live_connection_registers_on_connect_and_drops_on_disconnect(
    served: tuple[TaskService, str],
) -> None:
    service, base = served
    client = TaskServiceClient(httpx.Client(base_url=base, timeout=10.0))

    # Open the held host-liveness connection; the first tick means the server accepted it.
    conn = client.live_runner("host-1")
    next(conn)
    assert _wait_until(lambda: service.live_runners() == {"host-1"}), "runner never went live"
    assert client.live_runners() == ["host-1"]  # surfaced over REST too

    # Dropping the connection (a dying daemon) drops the runner immediately — no TTL.
    conn.close()
    assert _wait_until(lambda: not service.live_runners()), "runner not dropped on disconnect"


def test_hold_runner_liveness_loop_goes_live_then_drops_when_stopped(
    served: tuple[TaskService, str],
) -> None:
    # The daemon-side loop (what `main` runs in a thread): holds the connection while `running()`,
    # closing it on stop. Drive it in a thread and assert the runner goes live, then drops on stop.
    service, base = served
    client = TaskServiceClient(httpx.Client(base_url=base, timeout=10.0))
    running = True
    loop = threading.Thread(
        target=hold_runner_liveness,
        args=(client, "host-7"),
        kwargs={"running": lambda: running, "sleep": lambda _s: None},
        daemon=True,
    )
    loop.start()
    try:
        assert _wait_until(lambda: service.live_runners() == {"host-7"}), "loop never went live"
    finally:
        running = False
    assert _wait_until(lambda: not service.live_runners()), "loop did not drop the runner on stop"
    loop.join(timeout=5)


def test_reclaim_releases_only_the_dead_runners_non_terminal_claims(
    served: tuple[TaskService, str],
) -> None:
    service, base = served
    client = TaskServiceClient(httpx.Client(base_url=base, timeout=10.0))
    mine = client.create_task("r1", "spike")["id"]
    other = client.create_task("r1", "spike")["id"]
    client.claim(mine, "host-dead")
    client.claim(other, "host-live")

    reclaimed = client.reclaim_runner("host-dead")

    assert [t["id"] for t in reclaimed] == [mine]
    assert client.get_task(mine)["claimed_by"] is None  # released → respawnable by a healthy host
    assert client.get_task(other)["claimed_by"] == "host-live"  # another runner's claim untouched
    assert client.reclaim_runner("host-dead") == []  # idempotent: nothing left to release
