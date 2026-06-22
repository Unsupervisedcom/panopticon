"""The runnable task-service server (`python -m panopticon.taskservice`).

Exercises the default control-plane wiring via :func:`build_app` over an in-process
``TestClient`` — no socket bound, no uvicorn, no LLM. Proves the process entry point produces
a working app backed by the built-in workflows.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.taskservice.__main__ import build_app


def test_build_app_serves_default_wiring(tmp_path: Path) -> None:
    app = build_app(db="sqlite://", artifacts_root=str(tmp_path))  # in-memory DB; tmp artifacts
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}
    assert set(client.get("/workflows").json()) == {"spike", "github-peer-reviewed", "github-self-reviewed"}
