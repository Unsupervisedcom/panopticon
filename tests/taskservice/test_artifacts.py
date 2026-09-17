"""Filesystem artifact store + the shared id→path→URI resolver."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from panopticon.core.artifacts import (
    ArtifactStore,
    InvalidArtifactName,
    decode_segment,
    is_hidden,
    mcp_uri,
    repo_mcp_uri,
    validate_relative_name,
)
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore


def test_put_get_list_roundtrip(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("t1", "plan.md", b"# Plan\n"))
    asyncio.run(store.put("t1", "notes.md", b"notes"))
    assert asyncio.run(store.get("t1", "plan.md")) == b"# Plan\n"
    assert asyncio.run(store.list("t1")) == ["notes.md", "plan.md"]


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    assert asyncio.run(store.get("t1", "plan.md")) is None
    assert asyncio.run(store.list("t1")) == []


def test_path_returns_on_disk_path_or_none(tmp_path: Path) -> None:
    # path() is what co-located readers (the dashboard's open-in-place) use; it owns the layout.
    store = FilesystemArtifactStore(tmp_path)
    assert store.path("t1", "plan.md") is None  # absent
    asyncio.run(store.put("t1", "plan.md", b"# Plan\n"))
    path = store.path("t1", "plan.md")
    assert path == tmp_path / "tasks" / "t1" / "plan.md"
    assert path is not None and path.read_bytes() == b"# Plan\n"  # the real file, openable in place


def test_task_artifact_dir_returns_the_folder_or_none(tmp_path: Path) -> None:
    # path()'s directory twin — what the dashboard's `f` (open the folder) hands to the file
    # manager. The folder only exists once something has been written to the task.
    store = FilesystemArtifactStore(tmp_path)
    assert store.task_artifact_dir("t1") is None  # nothing written yet → nothing to open
    asyncio.run(store.put("t1", "plan.md", b"# Plan\n"))
    assert store.task_artifact_dir("t1") == tmp_path / "tasks" / "t1"


def test_task_artifact_dir_is_none_when_a_file_sits_at_the_path(tmp_path: Path) -> None:
    # Only a directory is openable as a folder: something else at that path is not the store's.
    store = FilesystemArtifactStore(tmp_path)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t1").write_text("not a directory")
    assert store.task_artifact_dir("t1") is None


def test_task_artifact_dir_rejects_traversal(tmp_path: Path) -> None:
    # The id is a single path segment, like every other accessor's.
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(InvalidArtifactName):
        store.task_artifact_dir("../escape")


def test_put_overwrites(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("t1", "plan.md", b"v1"))
    asyncio.run(store.put("t1", "plan.md", b"v2"))
    assert asyncio.run(store.get("t1", "plan.md")) == b"v2"


def test_rejects_traversal_in_name(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../evil", "a/b", "..", ""):
        with pytest.raises(InvalidArtifactName):
            asyncio.run(store.put("t1", bad, b"x"))


def test_allows_dotfile_names(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("t1", ".hidden", b"secret"))
    assert asyncio.run(store.get("t1", ".hidden")) == b"secret"
    assert asyncio.run(store.list("t1")) == [".hidden"]

    asyncio.run(store.put("t1", ".babysit-ci-state.json", b"{}"))
    names = asyncio.run(store.list("t1"))
    assert ".hidden" in names and ".babysit-ci-state.json" in names


def test_rejects_traversal_in_task_id(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(InvalidArtifactName):
        asyncio.run(store.put("..", "plan.md", b"x"))


def test_link_slug_aliases_the_task_dir(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("t1", "plan.md", b"# Plan\n"))
    asyncio.run(store.link_slug("t1", "fix-widget"))
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink()
    # The alias resolves to the same artifact the id-named dir holds.
    assert (alias / "plan.md").read_bytes() == b"# Plan\n"


def test_link_slug_target_is_relative(tmp_path: Path) -> None:
    # A relative target (the sibling id) keeps the whole root relocatable.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.link_slug("t1", "fix-widget"))
    assert (tmp_path / "tasks" / "fix-widget").readlink() == Path("t1")


def test_link_slug_creates_target_dir_when_absent(tmp_path: Path) -> None:
    # Slug can be set before any artifact is written; the alias must still resolve, not dangle.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.link_slug("t1", "fix-widget"))
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink() and alias.resolve().is_dir()
    asyncio.run(store.put("fix-widget", "notes.md", b"via the alias"))
    assert asyncio.run(store.get("t1", "notes.md")) == b"via the alias"  # same underlying dir


def test_link_slug_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.link_slug("t1", "fix-widget"))
    asyncio.run(store.link_slug("t1", "fix-widget"))  # no error, still a single valid link
    assert (tmp_path / "tasks" / "fix-widget").readlink() == Path("t1")


def test_relink_and_unlink_swap_the_alias(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("t1", "plan.md", b"# Plan\n"))
    asyncio.run(store.link_slug("t1", "old-name"))
    # Re-slug: point a new alias at the same task, drop the old one.
    asyncio.run(store.link_slug("t1", "new-name"))
    asyncio.run(store.unlink_slug("old-name"))
    assert not (tmp_path / "tasks" / "old-name").exists()
    assert (tmp_path / "tasks" / "new-name" / "plan.md").read_bytes() == b"# Plan\n"
    assert asyncio.run(store.get("t1", "plan.md")) == b"# Plan\n"  # the real dir is untouched


def test_unlink_slug_ignores_absent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.unlink_slug("never-linked"))  # no error


def test_link_slug_rejects_traversal(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../evil", "a/b", "..", ""):
        with pytest.raises(InvalidArtifactName):
            asyncio.run(store.link_slug("t1", bad))


def test_link_slug_refuses_to_clobber_a_real_entry(tmp_path: Path) -> None:
    # The astronomically-unlikely slug == real-task-id collision must not overwrite data.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("collision", "plan.md", b"real"))
    with pytest.raises(InvalidArtifactName):
        asyncio.run(store.link_slug("t1", "collision"))
    assert asyncio.run(store.get("collision", "plan.md")) == b"real"  # untouched


def test_mcp_uri_resolver() -> None:
    assert mcp_uri("t1", "plan.md") == "panopticon://tasks/t1/artifacts/plan.md"
    assert mcp_uri("t1", ".hidden") == "panopticon://tasks/t1/artifacts/.hidden"
    with pytest.raises(InvalidArtifactName):
        mcp_uri("t1", "../escape")


def test_mcp_uri_percent_encodes_reserved_characters() -> None:
    # A name with spaces or URI-reserved characters must yield a valid, unambiguous URI so the
    # resource handler can round-trip it — a raw space would otherwise be an invalid URI.
    assert mcp_uri("t1", "my notes.md") == "panopticon://tasks/t1/artifacts/my%20notes.md"
    assert mcp_uri("t1", "a+b&c.md") == "panopticon://tasks/t1/artifacts/a%2Bb%26c.md"


def test_decode_segment_reverses_mcp_uri_encoding() -> None:
    # The MCP layer captures template segments without decoding them, so the handler decodes.
    for name in ("plan.md", "my notes.md", "a+b&c.md", ".hidden"):
        encoded = mcp_uri("t1", name).rsplit("/", 1)[1]
        assert decode_segment(encoded) == name


def test_is_hidden_is_the_dotfile_rule() -> None:
    # One definition, shared by the dashboard's "show hidden" toggle and the task list's mark.
    assert is_hidden(".babysit-ci-state.json")
    assert not is_hidden("plan.md")
    assert not is_hidden("notes.tar.gz")


def test_has_unhidden_artifacts_ignores_hidden_and_missing(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("visible", "plan.md", b"# Plan"))
    asyncio.run(store.put("mixed", ".babysit-ci-state.json", b"{}"))
    asyncio.run(store.put("mixed", "notes.md", b"notes"))
    asyncio.run(store.put("hidden-only", ".babysit-ci-state.json", b"{}"))
    assert asyncio.run(store.has_unhidden_artifacts("visible"))
    assert asyncio.run(store.has_unhidden_artifacts("mixed"))  # hidden siblings don't mask it
    assert not asyncio.run(store.has_unhidden_artifacts("hidden-only"))
    # "absent" was never written at all — no directory on disk, and that's not an error.
    assert not asyncio.run(store.has_unhidden_artifacts("absent"))


def test_has_unhidden_artifacts_default_works_without_the_override() -> None:
    # An adapter with no cheaper way to answer inherits a correct implementation from the ABC:
    # the default is written in terms of list(), which every store must provide.
    class InMemoryStore(ArtifactStore):
        def __init__(self) -> None:
            self.files: dict[str, dict[str, bytes]] = {}
            self.repo_files: dict[str, dict[str, bytes]] = {}

        async def put(self, task_id: str, name: str, content: bytes) -> None:
            self.files.setdefault(task_id, {})[name] = content

        async def get(self, task_id: str, name: str) -> bytes | None:
            return self.files.get(task_id, {}).get(name)

        async def list(self, task_id: str) -> list[str]:
            return sorted(self.files.get(task_id, {}))

        # The repo-scoped half of the interface: unused here, but an adapter has to provide it.
        async def put_repo_artifact(self, repo_id: str, name: str, content: bytes) -> None:
            self.repo_files.setdefault(repo_id, {})[name] = content

        async def get_repo_artifact(self, repo_id: str, name: str) -> bytes | None:
            return self.repo_files.get(repo_id, {}).get(name)

        async def list_repo_artifacts(self, repo_id: str) -> list[str]:
            return sorted(self.repo_files.get(repo_id, {}))

    store = InMemoryStore()
    asyncio.run(store.put("visible", "plan.md", b"# Plan"))
    asyncio.run(store.put("hidden-only", ".state.json", b"{}"))
    assert asyncio.run(store.has_unhidden_artifacts("visible"))
    assert not asyncio.run(store.has_unhidden_artifacts("hidden-only"))
    assert not asyncio.run(store.has_unhidden_artifacts("absent"))


# -- repo-scoped artifacts ---------------------------------------------------------
#
# The same store, owned by a repo rather than a task: shared by every task in the repo, and its
# names may be nested (``notes/api.md``) where a task artifact's name is a single segment.


def test_repo_artifact_put_get_list_roundtrip(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put_repo_artifact("r1", "conventions.md", b"# How we work\n"))
    asyncio.run(store.put_repo_artifact("r1", "notes.md", b"notes"))
    assert asyncio.run(store.get_repo_artifact("r1", "conventions.md")) == b"# How we work\n"
    assert asyncio.run(store.list_repo_artifacts("r1")) == ["conventions.md", "notes.md"]
    assert (tmp_path / "repos" / "r1" / "notes.md").read_bytes() == b"notes"


def test_repo_artifact_missing_and_empty_repo(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    assert asyncio.run(store.get_repo_artifact("r1", "notes.md")) is None
    assert asyncio.run(store.list_repo_artifacts("r1")) == []  # no directory on disk, not an error


def test_repo_artifact_names_may_be_nested(tmp_path: Path) -> None:
    # Subdirectories are the point of the repo scope: a write creates the intermediate directories
    # and the listing recurses, returning the same ``/``-separated name a reader passes back in.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put_repo_artifact("r1", "notes/api/quirks.md", b"beware"))
    asyncio.run(store.put_repo_artifact("r1", "notes/ui.md", b"ui"))
    asyncio.run(store.put_repo_artifact("r1", "top.md", b"top"))
    assert asyncio.run(store.list_repo_artifacts("r1")) == [
        "notes/api/quirks.md",
        "notes/ui.md",
        "top.md",
    ]
    assert asyncio.run(store.get_repo_artifact("r1", "notes/api/quirks.md")) == b"beware"
    assert (tmp_path / "repos" / "r1" / "notes" / "api" / "quirks.md").is_file()


def test_repo_artifact_put_overwrites(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put_repo_artifact("r1", "notes/ui.md", b"v1"))
    asyncio.run(store.put_repo_artifact("r1", "notes/ui.md", b"v2"))
    assert asyncio.run(store.get_repo_artifact("r1", "notes/ui.md")) == b"v2"


def test_repo_artifact_rejects_traversal(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../evil", "a/../../evil", "/etc/passwd", "a//b", "a/", "a/.", "..", "", "a\\b"):
        with pytest.raises(InvalidArtifactName):
            asyncio.run(store.put_repo_artifact("r1", bad, b"x"))
    with pytest.raises(InvalidArtifactName):
        asyncio.run(store.put_repo_artifact("..", "notes.md", b"x"))


def test_repo_artifact_rejects_a_symlinked_escape(tmp_path: Path) -> None:
    # A name can look innocent and still land outside the repo directory when a subdirectory in
    # the tree is a symlink; only the resolved path shows it, which is why the store re-checks.
    store = FilesystemArtifactStore(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    repo_dir = tmp_path / "repos" / "r1"
    repo_dir.mkdir(parents=True)
    (repo_dir / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidArtifactName):
        asyncio.run(store.put_repo_artifact("r1", "escape/evil.md", b"x"))


def test_repo_and_task_namespaces_do_not_collide(tmp_path: Path) -> None:
    # A repo and a task may share an id; their artifacts are still separate documents.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put("shared-id", "plan.md", b"task"))
    asyncio.run(store.put_repo_artifact("shared-id", "plan.md", b"repo"))
    assert asyncio.run(store.get("shared-id", "plan.md")) == b"task"
    assert asyncio.run(store.get_repo_artifact("shared-id", "plan.md")) == b"repo"


def test_repo_artifact_path_and_dir(tmp_path: Path) -> None:
    # The two co-located accessors the dashboard opens with: the file (`e`) and the folder (`f`).
    store = FilesystemArtifactStore(tmp_path)
    assert store.repo_artifact_path("r1", "notes/ui.md") is None  # absent
    assert store.repo_artifact_dir("r1") is None  # nothing written yet → nothing to open
    asyncio.run(store.put_repo_artifact("r1", "notes/ui.md", b"ui"))
    path = store.repo_artifact_path("r1", "notes/ui.md")
    assert path == tmp_path / "repos" / "r1" / "notes" / "ui.md"
    assert path is not None and path.read_bytes() == b"ui"  # the real file, openable in place
    assert store.repo_artifact_dir("r1") == tmp_path / "repos" / "r1"


def test_repo_artifact_dir_is_not_a_file(tmp_path: Path) -> None:
    # A directory entry in the listing is not an artifact; only files are.
    store = FilesystemArtifactStore(tmp_path)
    asyncio.run(store.put_repo_artifact("r1", "notes/ui.md", b"ui"))
    assert asyncio.run(store.list_repo_artifacts("r1")) == ["notes/ui.md"]  # not "notes"


def test_validate_relative_name_accepts_nested_and_dotted_names() -> None:
    validate_relative_name("plan.md")
    validate_relative_name("notes/api/quirks.md")
    validate_relative_name(".state/ci.json")  # dot-prefixed is hidden, not invalid


def test_is_hidden_covers_a_dot_directory() -> None:
    # Hiding the directory but listing its contents would be incoherent, so any dot-prefixed
    # segment hides the artifact.
    assert is_hidden(".state.json")
    assert is_hidden(".state/ci.json")
    assert is_hidden("notes/.draft.md")
    assert not is_hidden("notes/api/quirks.md")


def test_repo_mcp_uri_encodes_a_nested_name_into_one_segment() -> None:
    # The MCP resource layer matches a template parameter within a single URI segment, so a nested
    # name's separators have to travel percent-encoded — and decode back to the same name.
    uri = repo_mcp_uri("r1", "notes/api quirks.md")
    assert uri == "panopticon://repos/r1/artifacts/notes%2Fapi%20quirks.md"
    assert decode_segment(uri.rsplit("/", 1)[1]) == "notes/api quirks.md"
    with pytest.raises(InvalidArtifactName):
        repo_mcp_uri("r1", "../evil")
