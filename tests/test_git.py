"""Local git ops: unit tests pin the emitted git commands for GitClones (clone/branch/set-origin)."""

from __future__ import annotations

from collections.abc import Sequence

from panopticon.core.git import GitClones, branch_name


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append((list(args), check))
        return ""


def test_branch_name_is_slug_derived() -> None:
    assert branch_name("fix-the-widget") == "panopticon/fix-the-widget"


def test_clone_local_emits_self_contained_clone() -> None:
    rec = _Recorder()
    GitClones(run=rec).clone_local(cache_path="/clones/r1", dest="/tasks/t1")
    assert rec.calls[0][0] == ["git", "clone", "--local", "/clones/r1", "/tasks/t1"]


def test_create_branch_and_set_origin() -> None:
    rec = _Recorder()
    git = GitClones(run=rec)
    git.create_branch(repo_path="/tasks/t1", branch="panopticon/fix-it")
    git.set_origin(repo_path="/tasks/t1", url="https://forge/r1.git")
    assert rec.calls[0][0] == ["git", "-C", "/tasks/t1", "checkout", "-b", "panopticon/fix-it"]
    assert rec.calls[1][0] == ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"]

