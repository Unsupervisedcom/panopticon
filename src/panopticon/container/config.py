"""The single place we touch claude's on-disk JSON config (`.claude.json`, `.claude/settings.json`).

A **read-merge-write** so a caller states only the keys it cares about and never clobbers the
rest: load whatever's already there (or start empty), let the caller mutate it in place, then
write it back with stable 2-space indentation. claude-specific, like its callers (M3 revisits for
other CLIs).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def update_json_config(path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """Read ``path`` as JSON (``{}`` if absent), apply ``mutate`` in place, write it back; return it.

    Creates ``path``'s parent directory if needed, so callers needn't pre-create the config dir.
    """
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    mutate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path
