"""The single place we touch an agent CLI's on-disk config files — JSON for claude
(`.claude.json`, `.claude/settings.json`) and TOML for codex (`.codex/config.toml`).

A **read-merge-write** so a caller states only the keys it cares about and never clobbers the
rest: load whatever's already there (or start empty), let the caller mutate it in the ``with``
block, then write it back on a clean exit. The JSON helper is claude's; the TOML helper is codex's
(ADR 0014 §1 asks for the analogous TOML merge). Both are shared across a CLI's adapter methods,
which each merge only their own keys into the one config file (codex's MCP block, hooks, and trust
posture all land in the same ``config.toml``).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tomli_w


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


@contextmanager
def update_toml_config(path: Path) -> Iterator[dict[str, Any]]:
    """Yield ``path``'s TOML (``{}`` if absent) to mutate in place, then write it back on clean exit.

    The TOML analogue of :func:`update_json_config` (ADR 0014 §1) — codex is configured via
    ``config.toml``, and several adapter methods each merge their own keys into it (the MCP server
    table, the turn-flip hooks, the trust/approval posture) without clobbering the others' or any
    codex wrote itself. Reads with the stdlib :mod:`tomllib` and writes with :mod:`tomli_w`; creates
    the parent directory on demand and, like the JSON helper, leaves the file untouched if the
    ``with`` block raises.
    """
    data: dict[str, Any] = tomllib.loads(path.read_text()) if path.exists() else {}
    yield data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))
