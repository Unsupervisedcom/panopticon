"""Per-user data and cache directories for panopticon (XDG Base Directory spec)."""
from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    """Return ``$XDG_DATA_HOME/panopticon`` (``~/.local/share/panopticon`` when unset).

    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "panopticon"


def user_cache_dir() -> Path:
    """Return ``$XDG_CACHE_HOME/panopticon`` (``~/.cache/panopticon`` when unset).

    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "panopticon"


def user_config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/panopticon`` (``~/.config/panopticon`` when unset).

    Does **not** create the directory — callers that write to it must mkdir themselves.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "panopticon"
