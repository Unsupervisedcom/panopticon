"""The runnable task-service server (`python -m panopticon.taskservice`).

Exercises the default control-plane wiring via :func:`build_app` over an in-process
``TestClient`` — no socket bound, no uvicorn, no LLM. Proves the process entry point produces
a working app backed by the built-in workflows.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.taskservice.__main__ import build_app
from panopticon.transport import make_http_client


def test_build_app_serves_default_wiring(tmp_path: Path) -> None:
    app = build_app(db="sqlite://", artifacts_root=str(tmp_path))  # in-memory DB; tmp artifacts
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}
    assert set(client.get("/workflows").json()) == {"spike", "parity"}


def test_service_answers_over_a_unix_socket(tmp_path: Path) -> None:
    """Socket mode end-to-end: bind a real UDS, then reach it through a ``unix://`` client.

    Proves the server's ``--socket`` bind and the client's UDS transport agree — the whole path
    ``make panopticon`` relies on, without provisioning a port. No Docker, no LLM.
    """
    app = build_app(db="sqlite://", artifacts_root=str(tmp_path))
    sock = tmp_path / "panopticon.sock"
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):  # the bind is async; wait for the socket to come up
            if server.started and sock.exists():
                break
            time.sleep(0.025)
        assert server.started, "uvicorn did not start on the socket"

        client = TaskServiceClient(make_http_client(f"unix://{sock}"))
        assert set(client.list_workflows()) == {"spike", "parity"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
