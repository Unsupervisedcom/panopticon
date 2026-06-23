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


def test_path_returns_on_disk_path_or_none(tmp_path: Path) -> None:
    # path() is what co-located readers (the dashboard's open-in-place) use; it owns the layout.
    store = FilesystemArtifactStore(tmp_path)
    assert store.path("t1", "plan.md") is None  # absent
    store.put("t1", "plan.md", b"# Plan\n")
    path = store.path("t1", "plan.md")
    assert path == tmp_path / "tasks" / "t1" / "plan.md"
    assert path is not None and path.read_bytes() == b"# Plan\n"  # the real file, openable in place


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


def test_link_slug_aliases_the_task_dir(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.put("t1", "plan.md", b"# Plan\n")
    store.link_slug("t1", "fix-widget")
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink()
    # The alias resolves to the same artifact the id-named dir holds.
    assert (alias / "plan.md").read_bytes() == b"# Plan\n"


def test_link_slug_target_is_relative(tmp_path: Path) -> None:
    # A relative target (the sibling id) keeps the whole root relocatable.
    store = FilesystemArtifactStore(tmp_path)
    store.link_slug("t1", "fix-widget")
    assert (tmp_path / "tasks" / "fix-widget").readlink() == Path("t1")


def test_link_slug_creates_target_dir_when_absent(tmp_path: Path) -> None:
    # Slug can be set before any artifact is written; the alias must still resolve, not dangle.
    store = FilesystemArtifactStore(tmp_path)
    store.link_slug("t1", "fix-widget")
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink() and alias.resolve().is_dir()
    store.put("fix-widget", "notes.md", b"via the alias")
    assert store.get("t1", "notes.md") == b"via the alias"  # same underlying dir


def test_link_slug_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.link_slug("t1", "fix-widget")
    store.link_slug("t1", "fix-widget")  # no error, still a single valid link
    assert (tmp_path / "tasks" / "fix-widget").readlink() == Path("t1")


def test_relink_and_unlink_swap_the_alias(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.put("t1", "plan.md", b"# Plan\n")
    store.link_slug("t1", "old-name")
    # Re-slug: point a new alias at the same task, drop the old one.
    store.link_slug("t1", "new-name")
    store.unlink_slug("old-name")
    assert not (tmp_path / "tasks" / "old-name").exists()
    assert (tmp_path / "tasks" / "new-name" / "plan.md").read_bytes() == b"# Plan\n"
    assert store.get("t1", "plan.md") == b"# Plan\n"  # the real dir is untouched


def test_unlink_slug_ignores_absent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    store.unlink_slug("never-linked")  # no error


def test_link_slug_rejects_traversal(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../evil", "a/b", "..", ".hidden", ""):
        with pytest.raises(InvalidArtifactName):
            store.link_slug("t1", bad)


def test_link_slug_refuses_to_clobber_a_real_entry(tmp_path: Path) -> None:
    # The astronomically-unlikely slug == real-task-id collision must not overwrite data.
    store = FilesystemArtifactStore(tmp_path)
    store.put("collision", "plan.md", b"real")
    with pytest.raises(InvalidArtifactName):
        store.link_slug("t1", "collision")
    assert store.get("collision", "plan.md") == b"real"  # untouched


def test_mcp_uri_resolver() -> None:
    assert mcp_uri("t1", "plan.md") == "panopticon://tasks/t1/artifacts/plan.md"
    with pytest.raises(InvalidArtifactName):
        mcp_uri("t1", "../escape")
