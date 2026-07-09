"""``python -m panopticon.taskservice`` — run the task service over HTTP.

Wires a default :class:`~panopticon.taskservice.service.TaskService` — on-disk SQLite + a
filesystem artifact store + the built-in workflows — into :func:`create_app` and serves it with
uvicorn. This is the LLM-free control plane's process entry point; runners and the terminal
controller are its clients (they reach it at ``PANOPTICON_SERVICE_URL``).

Workflows are **discovered**, not hardcoded: the built-in :mod:`panopticon.workflows` package,
``$XDG_CONFIG_HOME/panopticon/workflows/`` (``~/.config/panopticon/workflows/`` when unset,
if it exists), and an optional ``--workflows-path`` directory (ADR 0004, Slice 8) — so adding a
workflow is just dropping a module in ``~/.config/panopticon/workflows/`` with no install step.
Config comes from flags or ``PANOPTICON_*`` env, with on-disk defaults so a bare
``python -m panopticon.taskservice`` persists across restarts.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from panopticon.core.dirs import user_data_dir
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import DEFAULT_ARTIFACTS, FilesystemArtifactStore
from panopticon.taskservice.layers_fs import DEFAULT_LAYERS, FilesystemLayerStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows.discovery import discover_workflows

DEFAULT_DB: str = "sqlite:///" + str(user_data_dir() / "panopticon.db")

_SQLITE_PREFIX = "sqlite:///"


def migrate_db_to_home(db_url: str) -> None:
    """Migrate legacy DB locations to the XDG data dir on first start after upgrading.

    Handles two upgrade hops in order:
    - ``./panopticon.db`` (pre-#251) → XDG
    - ``~/.panopticon/panopticon.db`` (#251) → XDG

    Only acts when *db_url* is the XDG default (not a custom override) and the destination
    doesn't exist. Idempotent, safe to call every start. Called from both ``main()`` and
    ``migrations/env.py`` so it fires before alembic connects too.
    """
    if db_url != DEFAULT_DB:
        return
    new = Path(db_url[len(_SQLITE_PREFIX):])
    if new.exists():
        return
    for old in [Path("panopticon.db"), Path.home() / ".panopticon" / "panopticon.db"]:
        if old.is_file():
            logging.info("panopticon: migrating %s → %s", old.resolve(), new)
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            return


def _migrate_legacy_to_home(db: str, artifacts: str, layers: str) -> None:
    """Migrate all legacy runtime data to XDG locations on first start after upgrading.

    Covers the DB (delegated to :func:`migrate_db_to_home`), artifact store, layer store, and
    the operator-authored config dirs (hooks, secrets). Tries both legacy source locations in
    order (CWD-relative pre-#251, then ``~/.panopticon/`` post-#251) for each path. Skips each
    path when a custom override is in use, when no legacy source exists, or when the destination
    already exists (no clobbering).

    Note: artifacts go to ``$XDG_DATA_HOME/panopticon/`` (runtime-generated data); layers,
    hooks, and secrets go to ``$XDG_CONFIG_HOME/panopticon/`` (operator-authored config).
    """
    from panopticon.core.dirs import user_config_dir as _config_dir

    migrate_db_to_home(db)

    if artifacts == DEFAULT_ARTIFACTS:
        new = Path(artifacts)
        if not new.exists():
            for old in [Path("artifacts"), Path.home() / ".panopticon" / "artifacts"]:
                if old.is_dir():
                    logging.info("panopticon: migrating %s → %s", old.resolve(), new)
                    shutil.move(str(old), str(new))
                    break

    if layers == DEFAULT_LAYERS:
        new = Path(layers)
        if not new.exists():
            for old in [Path("layers"), Path.home() / ".panopticon" / "layers"]:
                if old.is_dir():
                    logging.info("panopticon: migrating %s → %s", old.resolve(), new)
                    shutil.move(str(old), str(new))
                    break

    for name in ("hooks", "secrets"):
        new = _config_dir() / name
        if not new.exists():
            old = Path.home() / ".panopticon" / name
            if old.is_dir():
                logging.info("panopticon: migrating %s → %s", old.resolve(), new)
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))


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
    user_data_dir().mkdir(parents=True, exist_ok=True)
    _migrate_legacy_to_home(args.db, args.artifacts, args.layers)
    app = build_app(
        db=args.db, artifacts_root=args.artifacts, layers_root=args.layers,
        workflows_path=args.workflows_path,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
