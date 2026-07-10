"""Per-user data, cache, and config directories for panopticon (XDG Base Directory spec).

Resolution order for each directory:
  ``PANOPTICON_DATA/CACHE/CONFIG`` (top-level override) →
  ``XDG_DATA_HOME/XDG_CACHE_HOME/XDG_CONFIG_HOME`` →
  ``~/.local/share`` / ``~/.cache`` / ``~/.config``
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
