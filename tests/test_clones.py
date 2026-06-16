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
    cache = CloneCache("/clones", run=rec, exists=lambda _p: False)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [["git", "clone", "https://x/r1.git", "/clones/r1"]]


def test_fetches_when_the_clone_exists() -> None:
    rec = _Recorder()
    cache = CloneCache("/clones", run=rec, exists=lambda _p: True)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [["git", "-C", "/clones/r1", "fetch", "--all", "--prune"]]  # kept current


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
    assert cache.ensure("r1", str(origin)) == path  # second use: fetches, same path, no error
