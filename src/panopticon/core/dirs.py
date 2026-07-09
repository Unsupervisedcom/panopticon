"""Per-user data directory for panopticon (XDG Base Directory spec)."""
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
