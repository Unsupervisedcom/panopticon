"""Startup migration helpers for the session service (shared by ``__main__`` and ``host``)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from panopticon.core.dirs import user_cache_dir, user_data_dir

_log = logging.getLogger(__name__)

#: Per-host provisioning roots (ADR 0010/0011): the per-repo clone cache and the per-task clones.
DEFAULT_CLONE_CACHE_ROOT: str = str(user_cache_dir() / "repos")
DEFAULT_TASKS_ROOT: str = str(user_data_dir() / "tasks")


def migrate_session_dirs(clone_cache_root: str, tasks_root: str) -> None:
    """Migrate legacy cache/tasks dirs to XDG locations.

    Tries CWD-relative paths (pre-#251) then ``~/.panopticon/`` (#251) as sources.
    Skips when a custom override is in use or the destination already exists.
    """
    if clone_cache_root == DEFAULT_CLONE_CACHE_ROOT:
        new = Path(clone_cache_root)
        if not new.exists():
            for old in [Path("cache"), Path.home() / ".panopticon" / "cache"]:
                if old.is_dir():
                    _log.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break

    if tasks_root == DEFAULT_TASKS_ROOT:
        new = Path(tasks_root)
        if not new.exists():
            for old in [Path("tasks"), Path.home() / ".panopticon" / "tasks"]:
                if old.is_dir():
                    _log.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break
