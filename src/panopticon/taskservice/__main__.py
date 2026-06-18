"""``python -m panopticon.taskservice`` — run the task service over HTTP.

Wires a default :class:`~panopticon.taskservice.service.TaskService` — on-disk SQLite + a
filesystem artifact store + the built-in workflows — into :func:`create_app` and serves it with
uvicorn. This is the LLM-free control plane's process entry point; runners and the terminal
controller are its clients (they reach it at ``PANOPTICON_SERVICE_URL``).

Workflows are **discovered**, not hardcoded: the built-in :mod:`panopticon.workflows` package plus
an optional ``--workflows-path`` directory (ADR 0004, Slice 8) — so adding a workflow is just
dropping a module on a scanned path. Config comes from flags or ``PANOPTICON_*`` env, with on-disk
defaults so a bare ``python -m panopticon.taskservice`` persists across restarts.

**Networking.** The service binds a TCP host/port by default. Pass ``--socket <path>`` (or set
``PANOPTICON_SOCKET``) to bind a **Unix-domain socket** instead — no port is provisioned. That's
what ``make panopticon`` uses: the whole system shares one host, the runner bind-mounts the socket
into each task container, and every client reaches it via a ``unix://<path>`` service URL (see
:mod:`panopticon.transport`).
"""

from __future__ import annotations

import argparse
import contextlib
import os
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.discovery import discover_workflows

DEFAULT_DB = "sqlite:///panopticon.db"
DEFAULT_ARTIFACTS = "./artifacts"


def build_app(
    *, db: str = DEFAULT_DB, artifacts_root: str = DEFAULT_ARTIFACTS, workflows_path: str | None = None
) -> FastAPI:
    """Build the task-service app around the default control-plane wiring (no LLM).

    Workflows are discovered from the built-in package plus an optional ``workflows_path`` dir.
    """
    service = TaskService(
        SqlAlchemyStore(db),
        discover_workflows(path=workflows_path),
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
    parser.add_argument(
        "--socket",
        default=os.environ.get("PANOPTICON_SOCKET"),
        help="bind a Unix-domain socket at this path instead of host/port (no port provisioned)",
    )
    parser.add_argument("--db", default=os.environ.get("PANOPTICON_DB", DEFAULT_DB))
    parser.add_argument(
        "--artifacts", default=os.environ.get("PANOPTICON_ARTIFACTS", DEFAULT_ARTIFACTS)
    )
    parser.add_argument(
        "--workflows-path",
        default=os.environ.get("PANOPTICON_WORKFLOWS_PATH"),
        help="extra directory to discover Workflow subclasses in (beyond the built-ins)",
    )
    args = parser.parse_args(argv)
    app = build_app(db=args.db, artifacts_root=args.artifacts, workflows_path=args.workflows_path)
    if args.socket:  # bind a Unix socket (make panopticon); no port provisioned
        # Clear a stale socket file left by an unclean prior shutdown so the rebind succeeds.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(args.socket)
        uvicorn.run(app, uds=args.socket)
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
