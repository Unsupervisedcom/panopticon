"""The claude JSON-config helper: a read-merge-write that never clobbers keys it didn't set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from panopticon.container.config import update_json_config


def test_update_json_config_starts_empty_when_absent(tmp_path: Path) -> None:
    path = update_json_config(tmp_path / "nested" / "config.json", lambda d: d.update({"a": 1}))
    assert path == tmp_path / "nested" / "config.json"  # parent dir created on demand
    assert json.loads(path.read_text()) == {"a": 1}


def test_update_json_config_merges_into_existing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"keep": "me", "override": "old"}')

    def mutate(data: dict[str, Any]) -> None:
        data["override"] = "new"
        data["added"] = True

    update_json_config(path, mutate)

    assert json.loads(path.read_text()) == {"keep": "me", "override": "new", "added": True}
