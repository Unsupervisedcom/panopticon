"""``python -m panopticon.taskservice`` — run the task service over HTTP.

Wires a default :class:`~panopticon.taskservice.service.TaskService` — on-disk SQLite + a
filesystem artifact store + the built-in workflows — into :func:`create_app` and serves it with
uvicorn. This is the LLM-free control plane's process entry point; runners and the terminal
controller are its clients (they reach it at ``PANOPTICON_SERVICE_URL``).

Workflows are **discovered**, not hardcoded: the built-in :mod:`panopticon.workflows` package plus
an optional ``--workflows-path`` directory (ADR 0004, Slice 8) — so adding a workflow is just
dropping a module on a scanned path. Config comes from flags or ``PANOPTICON_*`` env, with on-disk
defaults so a bare ``python -m panopticon.taskservice`` persists across restarts.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import DEFAULT_ARTIFACTS, FilesystemArtifactStore
from panopticon.taskservice.layers_fs import DEFAULT_LAYERS, FilesystemLayerStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.discovery import discover_workflows

DEFAULT_DB = "sqlite:///panopticon.db"


def build_app(
    *,
    db: str = DEFAULT_DB,
    artifacts_root: str = DEFAULT_ARTIFACTS,
    layers_root: str = DEFAULT_LAYERS,
    workflows_path: str | None = None,
) -> FastAPI:
    """Build the task-service app around the default control-plane wiring (no LLM).

    Workflows are discovered from the built-in package plus an optional ``workflows_path`` dir.
    Repo image layers are read as files under ``layers_root`` (served over REST, ADR 0005).
    """
    service = TaskService(
        SqlAlchemyStore(db),
        discover_workflows(path=workflows_path),
        FilesystemArtifactStore(artifacts_root),
        layers=FilesystemLayerStore(layers_root),
    )
    return create_app(service)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    parser.add_argument(
        "--layers", default=os.environ.get("PANOPTICON_LAYERS", DEFAULT_LAYERS),
        help="directory of repo Dockerfile layer files (referenced by Repo.image_layer_file)",
    )
    parser.add_argument(
        "--workflows-path",
        default=os.environ.get("PANOPTICON_WORKFLOWS_PATH"),
        help="extra directory to discover Workflow subclasses in (beyond the built-ins)",
    )
    args = parser.parse_args(argv)
    app = build_app(
        db=args.db, artifacts_root=args.artifacts, layers_root=args.layers,
        workflows_path=args.workflows_path,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
