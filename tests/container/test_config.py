"""The config read-merge-write helpers (JSON for claude, TOML for codex): never clobber a key we
didn't set."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from panopticon.container.config import update_json_config, update_toml_config


def test_update_json_config_starts_empty_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    with update_json_config(path) as data:  # parent dir created on demand
        data["a"] = 1
    assert json.loads(path.read_text()) == {"a": 1}


def test_update_json_config_merges_into_existing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"keep": "me", "override": "old"}')

    with update_json_config(path) as data:
        data["override"] = "new"
        data["added"] = True

    assert json.loads(path.read_text()) == {"keep": "me", "override": "new", "added": True}


def test_update_json_config_leaves_file_untouched_on_error(tmp_path: Path) -> None:
    # A raise inside the block aborts the write — no half-applied config lands on disk.
    path = tmp_path / "config.json"
    path.write_text('{"keep": "me"}')

    with pytest.raises(RuntimeError), update_json_config(path) as data:
        data["added"] = True
        raise RuntimeError("boom")

    assert json.loads(path.read_text()) == {"keep": "me"}  # unchanged


# -- the TOML helper (codex's config.toml) ------------------------------------------------------


def test_update_toml_config_starts_empty_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    with update_toml_config(path) as data:  # parent dir created on demand
        data["mcp_servers"] = {"panopticon": {"url": "http://svc/mcp"}}
    assert tomllib.loads(path.read_text()) == {
        "mcp_servers": {"panopticon": {"url": "http://svc/mcp"}}
    }


def test_update_toml_config_merges_into_existing(tmp_path: Path) -> None:
    # Codex's MCP table, trust, and approval posture each merge into the one config.toml — a later
    # writer must not clobber an earlier one's keys (nor anything codex wrote itself).
    path = tmp_path / "config.toml"
    path.write_text(
        'approval_policy = "never"\n\n[mcp_servers.panopticon]\nurl = "http://svc/mcp"\n'
    )

    with update_toml_config(path) as data:
        data["sandbox_mode"] = "danger-full-access"
        data.setdefault("projects", {})["/workspace"] = {"trust_level": "trusted"}

    data = tomllib.loads(path.read_text())
    assert data["approval_policy"] == "never"  # preserved
    assert data["mcp_servers"]["panopticon"] == {"url": "http://svc/mcp"}  # preserved
    assert data["sandbox_mode"] == "danger-full-access"
    assert data["projects"]["/workspace"]["trust_level"] == "trusted"


def test_update_toml_config_leaves_file_untouched_on_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('keep = "me"\n')

    with pytest.raises(RuntimeError), update_toml_config(path) as data:
        data["added"] = True
        raise RuntimeError("boom")

    assert tomllib.loads(path.read_text()) == {"keep": "me"}  # unchanged
