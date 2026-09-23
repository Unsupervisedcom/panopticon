"""Per-repo clone cache (ADR 0010): unit tests pin the emitted `git` and the clone-vs-fetch
decision (fakes); one integration test clones a real local repo (skipped when git is absent)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.clones import CloneCache


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        return ""


def test_path_is_repo_scoped_under_root() -> None:
    assert CloneCache("/clones/").path("r1") == "/clones/r1"  # trailing slash normalized


def test_clones_on_first_use() -> None:
    rec = _Recorder()
    cache = CloneCache("/clones", run=rec, exists=lambda _p: False, makedirs=lambda _p: None)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [["git", "clone", "https://x/r1.git", "/clones/r1"]]


def test_fetches_when_the_clone_exists() -> None:
    rec = _Recorder()
    cache = CloneCache("/clones", run=rec, exists=lambda _p: True, makedirs=lambda _p: None)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [
        ["git", "-C", "/clones/r1", "fetch", "--all", "--prune"],
        # advance local base to upstream (else stale) — an ordinary merge, not --ff-only, so a
        # diverged-but-unconflicting cache clone still catches up; identity is passed inline so a
        # merge commit doesn't need the host's global git config.
        [
            "git",
            "-C",
            "/clones/r1",
            "-c",
            "user.name=panopticon",
            "-c",
            "user.email=panopticon@localhost",
            "merge",
            "--no-edit",
        ],
    ]


def test_a_conflicting_merge_is_aborted_and_raised() -> None:
    """A failed merge must not leave the cache clone mid-merge — the next task clones from it."""
    calls: list[list[str]] = []

    def run(args: Sequence[str], *, check: bool = True) -> str:
        calls.append(list(args))
        if "merge" in args and "--no-edit" in args:
            raise RuntimeError("CONFLICT")
        return ""

    cache = CloneCache("/clones", run=run, exists=lambda _p: True, makedirs=lambda _p: None)
    with pytest.raises(RuntimeError):
        cache.ensure("r1", "https://x/r1.git")
    assert calls[-1] == ["git", "-C", "/clones/r1", "merge", "--abort"]


def test_ensure_creates_root_dir_before_cloning(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    assert not root.exists()
    cache = CloneCache(str(root), run=lambda *_a, **_kw: "", exists=lambda _p: False)
    cache.ensure("r1", "https://x/r1.git")
    assert root.is_dir()


# -- integration: a real git repo ---------------------------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_ensure_clones_then_fetches_a_real_repo(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)
    run("git", "init", "--initial-branch", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (origin / "README").write_text("hi")
    run("git", "add", "--all")
    run("git", "commit", "--message", "init")

    cache = CloneCache(str(tmp_path / "clones"))
    path = cache.ensure("r1", str(origin))  # first use: clones
    assert (Path(path) / "README").read_text() == "hi"

    (origin / "README").write_text("updated")  # origin's base branch moves forward
    run("git", "commit", "--all", "--message", "second")
    assert cache.ensure("r1", str(origin)) == path  # second use: fetch + fast-forward, same path
    assert (
        Path(path) / "README"
    ).read_text() == "updated"  # the cache's base branch advanced (not stale)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_ensure_merges_a_diverged_but_unconflicting_clone(tmp_path: Path) -> None:
    """The cache clone has a local commit of its own; upstream moved a *different* file.

    `--ff-only` would refuse this; an ordinary merge takes both sides.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    at_origin = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)
    at_origin("git", "init", "--initial-branch", "main")
    at_origin("git", "config", "user.email", "t@example.com")
    at_origin("git", "config", "user.name", "t")
    (origin / "README").write_text("hi")
    at_origin("git", "add", "--all")
    at_origin("git", "commit", "--message", "init")

    cache = CloneCache(str(tmp_path / "clones"))
    path = Path(cache.ensure("r1", str(origin)))

    at_clone = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True)
    at_clone("git", "config", "user.email", "c@example.com")
    at_clone("git", "config", "user.name", "c")
    (path / "LOCAL").write_text("local work")  # the clone diverges…
    at_clone("git", "add", "--all")
    at_clone("git", "commit", "--message", "local")

    (origin / "OTHER").write_text("upstream work")  # …and so does origin, without conflicting
    at_origin("git", "add", "--all")
    at_origin("git", "commit", "--message", "upstream")

    assert cache.ensure("r1", str(origin)) == str(path)
    assert (path / "LOCAL").read_text() == "local work"  # both sides survive the merge
    assert (path / "OTHER").read_text() == "upstream work"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_ensure_aborts_a_conflicting_merge_leaving_the_clone_usable(tmp_path: Path) -> None:
    """A real conflict raises, and the clone is left clean — not mid-merge."""
    origin = tmp_path / "origin"
    origin.mkdir()
    at_origin = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)
    at_origin("git", "init", "--initial-branch", "main")
    at_origin("git", "config", "user.email", "t@example.com")
    at_origin("git", "config", "user.name", "t")
    (origin / "README").write_text("hi")
    at_origin("git", "add", "--all")
    at_origin("git", "commit", "--message", "init")

    cache = CloneCache(str(tmp_path / "clones"))
    path = Path(cache.ensure("r1", str(origin)))

    at_clone = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True)
    at_clone("git", "config", "user.email", "c@example.com")
    at_clone("git", "config", "user.name", "c")
    (path / "README").write_text("clone side")  # both sides edit the same line
    at_clone("git", "commit", "--all", "--message", "local")

    (origin / "README").write_text("origin side")
    at_origin("git", "commit", "--all", "--message", "upstream")

    with pytest.raises(subprocess.CalledProcessError):
        cache.ensure("r1", str(origin))
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""  # the aborted merge left no conflicted files behind
    assert (path / "README").read_text() == "clone side"
