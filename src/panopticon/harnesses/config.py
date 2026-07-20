"""The single place harness renderers touch a CLI's on-disk JSON config (e.g. `.claude.json`,
`.claude/settings.json`, codex's `auth.json`).

A **read-merge-write** so a caller states only the keys it cares about and never clobbers the
rest: load whatever's already there (or start empty), let the caller mutate it in the ``with``
block, then write it back with stable 2-space indentation on a clean exit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def update_json_config(path: Path) -> Iterator[dict[str, Any]]:
    """Yield ``path``'s JSON (``{}`` if absent) to mutate in place, then write it back on clean exit.

    Creates ``path``'s parent directory if needed, so callers needn't pre-create the config dir. If
    the ``with`` block raises, the file is left untouched — no half-written config.
    """
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    yield data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
