"""Filesystem artifact store + the shared id→path→URI resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.core.artifacts import InvalidArtifactName, mcp_uri
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore


def test_put_get_list_roundtrip(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.put("t1", "plan.md", b"# Plan\n")
    store.put("t1", "notes.md", b"notes")
    assert store.get("t1", "plan.md") == b"# Plan\n"
    assert store.list("t1") == ["notes.md", "plan.md"]


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    assert store.get("t1", "plan.md") is None
    assert store.list("t1") == []


def test_put_overwrites(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.put("t1", "plan.md", b"v1")
    store.put("t1", "plan.md", b"v2")
    assert store.get("t1", "plan.md") == b"v2"


def test_rejects_traversal_in_name(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../evil", "a/b", "..", ".hidden", ""):
        with pytest.raises(InvalidArtifactName):
            store.put("t1", bad, b"x")


def test_rejects_traversal_in_task_id(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(InvalidArtifactName):
        store.put("..", "plan.md", b"x")


def test_mcp_uri_resolver() -> None:
    assert mcp_uri("t1", "plan.md") == "panopticon://tasks/t1/artifacts/plan.md"
    with pytest.raises(InvalidArtifactName):
        mcp_uri("t1", "../escape")
