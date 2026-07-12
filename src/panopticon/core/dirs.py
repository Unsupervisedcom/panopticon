"""Per-user directories for panopticon (XDG Base Directory spec) and the paths derived from them.

Two layers live here. The ``user_*_dir()`` functions **resolve the three base directories** by
walking the override chain:

  ``PANOPTICON_DATA/CACHE/CONFIG`` (top-level override) →
  ``XDG_DATA_HOME/XDG_CACHE_HOME/XDG_CONFIG_HOME`` →
  ``~/.local/share`` / ``~/.cache`` / ``~/.config``

The module-level constants below then **compose panopticon's well-known sub-paths** onto those
bases (artifacts, tasks, the clone cache, layer files). Setting one base-dir variable moves
its entire subtree — there are no per-path overrides. Code that needs a base for an ad-hoc subpath
not listed here (e.g. ``workflows/``, ``hooks/``) calls the ``user_*_dir()`` functions directly.
"""
from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    """Return the panopticon data directory (``~/.local/share/panopticon`` by default).

    Resolution: ``$PANOPTICON_DATA`` → ``$XDG_DATA_HOME/panopticon`` → ``~/.local/share/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_DATA")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "panopticon"


def user_cache_dir() -> Path:
    """Return the panopticon cache directory (``~/.cache/panopticon`` by default).

    Resolution: ``$PANOPTICON_CACHE`` → ``$XDG_CACHE_HOME/panopticon`` → ``~/.cache/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_CACHE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "panopticon"


def user_config_dir() -> Path:
    """Return the panopticon config directory (``~/.config/panopticon`` by default).

    Resolution: ``$PANOPTICON_CONFIG`` → ``$XDG_CONFIG_HOME/panopticon`` → ``~/.config/panopticon``.
    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    override = os.environ.get("PANOPTICON_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "panopticon"


#: Task artifact store — $PANOPTICON_DATA/artifacts
ARTIFACTS_DIR: str = str(user_data_dir() / "artifacts")

#: Per-task workspace clones — $PANOPTICON_DATA/tasks
TASKS_DIR: str = str(user_data_dir() / "tasks")

#: Per-repo clone cache — $PANOPTICON_CACHE/repos
CLONE_CACHE_DIR: str = str(user_cache_dir() / "repos")

#: Operator-authored Dockerfile layer files — $PANOPTICON_CONFIG/layers
LAYERS_DIR: str = str(user_config_dir() / "layers")
