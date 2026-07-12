"""Startup migration helpers for the session service (shared by ``__main__`` and ``host``)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from panopticon.core.dirs import CLONE_CACHE_DIR, TASKS_DIR

_log = logging.getLogger(__name__)


def migrate_session_dirs(clone_cache_root: str, tasks_root: str) -> None:
    """Migrate legacy cache/tasks dirs to XDG locations.

    Tries CWD-relative paths (pre-#251) then ``~/.panopticon/`` (#251) as sources.
    Skips when a custom override is in use or the destination already exists.
    """
    if clone_cache_root == CLONE_CACHE_DIR:
        new = Path(clone_cache_root)
        if not new.exists():
            for old in [Path("cache"), Path.home() / ".panopticon" / "cache"]:
                if old.is_dir():
                    _log.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break

    if tasks_root == TASKS_DIR:
        new = Path(tasks_root)
        if not new.exists():
            for old in [Path("tasks"), Path.home() / ".panopticon" / "tasks"]:
                if old.is_dir():
                    _log.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break
