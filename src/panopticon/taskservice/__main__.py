"""``python -m panopticon.taskservice`` — run the task service over HTTP.

Wires a default :class:`~panopticon.taskservice.service.TaskService` — on-disk SQLite + a
filesystem artifact store + the built-in workflows — into :func:`create_app` and serves it with
uvicorn. This is the LLM-free control plane's process entry point; runners and the terminal
controller are its clients (they reach it at ``PANOPTICON_SERVICE_URL``).

This is the minimal "it runs" seam: path-based workflow registration and the mounted MCP HTTP
app are the fuller Slice 7a. Config comes from flags or ``PANOPTICON_*`` env, with on-disk
defaults so a bare ``python -m panopticon.taskservice`` persists across restarts.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Parity, Spike

DEFAULT_DB = "sqlite:///panopticon.db"
DEFAULT_ARTIFACTS = "./artifacts"


def build_app(*, db: str = DEFAULT_DB, artifacts_root: str = DEFAULT_ARTIFACTS) -> FastAPI:
    """Build the task-service app around the default control-plane wiring (no LLM)."""
    service = TaskService(
        SqlAlchemyStore(db),
        {wf.name: wf for wf in (Spike(), Parity())},
        FilesystemArtifactStore(artifacts_root),
    )
    return create_app(service)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m panopticon.taskservice", description="Run the task service over HTTP."
    )
    parser.add_argument("--host", default=os.environ.get("PANOPTICON_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PANOPTICON_PORT", "8000"))
    )
    parser.add_argument("--db", default=os.environ.get("PANOPTICON_DB", DEFAULT_DB))
    parser.add_argument(
        "--artifacts", default=os.environ.get("PANOPTICON_ARTIFACTS", DEFAULT_ARTIFACTS)
    )
    args = parser.parse_args(argv)
    uvicorn.run(build_app(db=args.db, artifacts_root=args.artifacts), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
