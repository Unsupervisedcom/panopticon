"""Local git worktree ops: unit tests pin the emitted git commands + slug-gating; one
integration test exercises a real repo (skipped when git is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.git import GitWorktrees, Worktree, branch_name, worktree_path


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append((list(args), check))
        return ""


def test_naming_is_slug_derived() -> None:
    assert branch_name("fix-the-widget") == "panopticon/fix-the-widget"
    assert worktree_path("/wt/", "r1", "panopticon/fix-the-widget") == "/wt/r1/panopticon/fix-the-widget"


def test_create_emits_worktree_add_and_returns_branch_and_path() -> None:
    rec = _Recorder()
    wt = GitWorktrees(run=rec).create(
        repo_path="/repos/r1", worktrees_root="/wt", repo_id="r1", slug="fix-it", base="main"
    )
    assert wt == Worktree(branch="panopticon/fix-it", path="/wt/r1/panopticon/fix-it")
    (cmd, _), = rec.calls
    assert cmd == [
        "git", "-C", "/repos/r1", "worktree", "add",
        "-b", "panopticon/fix-it", "/wt/r1/panopticon/fix-it", "main",
    ]


def test_create_is_slug_gated() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match="slug"):
        GitWorktrees(run=rec).create(
            repo_path="/r", worktrees_root="/wt", repo_id="r1", slug=None, base="main"
        )
    assert rec.calls == []  # nothing run before the slug exists


def test_remove_force_tier_and_idempotent() -> None:
    rec = _Recorder()
    git = GitWorktrees(run=rec)
    git.remove(repo_path="/r", worktree_path="/wt/r1/panopticon/fix-it")
    git.remove(repo_path="/r", worktree_path="/wt/r1/panopticon/fix-it", force=True)
    assert rec.calls[0] == (["git", "-C", "/r", "worktree", "remove", "/wt/r1/panopticon/fix-it"], False)
    assert rec.calls[1][0][-1] == "--force"
    assert rec.calls[1][1] is False  # idempotent: never raises on an already-gone worktree


# -- integration: a real git repo ---------------------------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_create_and_remove_a_real_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README").write_text("hi")
    run("git", "add", "-A")
    run("git", "commit", "-m", "init")

    git = GitWorktrees()
    wt = git.create(
        repo_path=str(repo), worktrees_root=str(tmp_path / "wt"), repo_id="r1", slug="fix-it", base="main"
    )
    assert Path(wt.path).is_dir()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "panopticon/fix-it"], capture_output=True, text=True
    ).stdout
    assert "panopticon/fix-it" in branches

    git.remove(repo_path=str(repo), worktree_path=wt.path, force=True)
    assert not Path(wt.path).exists()
