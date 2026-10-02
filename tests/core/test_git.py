"""Local git worktree ops: unit tests pin the emitted git commands + slug-gating; one
integration test exercises a real repo (skipped when git is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.core.git import (
    SUBMODULE_CONFLICTED,
    SUBMODULE_CURRENT,
    SUBMODULE_MODIFIED,
    SUBMODULE_UNINITIALIZED,
    GitClones,
    GitError,
    GitWorktrees,
    Worktree,
    branch_name,
    is_forge_url,
    local_repo_path,
    parse_submodule_paths,
    parse_submodule_status,
    worktree_path,
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append((list(args), check))
        return ""


def test_naming_is_slug_derived() -> None:
    assert branch_name("fix-the-widget") == "panopticon/fix-the-widget"
    assert (
        worktree_path("/wt/", "r1", "panopticon/fix-the-widget")
        == "/wt/r1/panopticon/fix-the-widget"
    )


def test_create_emits_worktree_add_and_returns_branch_and_path() -> None:
    rec = _Recorder()
    wt = GitWorktrees(run=rec).create(
        repo_path="/repos/r1", worktrees_root="/wt", repo_id="r1", slug="fix-it", base="main"
    )
    assert wt == Worktree(branch="panopticon/fix-it", path="/wt/r1/panopticon/fix-it")
    ((cmd, _),) = rec.calls
    assert cmd == [
        "git",
        "-C",
        "/repos/r1",
        "worktree",
        "add",
        "-b",
        "panopticon/fix-it",
        "/wt/r1/panopticon/fix-it",
        "main",
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
    assert rec.calls[0] == (
        ["git", "-C", "/r", "worktree", "remove", "/wt/r1/panopticon/fix-it"],
        False,
    )
    assert rec.calls[1][0][-1] == "--force"
    assert rec.calls[1][1] is False  # idempotent: never raises on an already-gone worktree


# -- per-task local clones (ADR 0011) -----------------------------------------------


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
    assert rec.calls[1][0] == [
        "git",
        "-C",
        "/tasks/t1",
        "remote",
        "set-url",
        "origin",
        "https://forge/r1.git",
    ]


def test_submodule_status_parses_a_state_per_submodule() -> None:
    output = (
        "-abc123 vendor/lib\n"  # uninitialized: no ` (describe)` suffix
        "+def456 vendor/other (heads/main)\n"  # at a commit other than the gitlink's
        " 789abc vendor/other/nested (v1.2.3)\n"  # --recursive descended into it
        "Uc0ffee vendor/conflicted (heads/main)\n"
    )

    def _status(args: Sequence[str], *, check: bool = True) -> str:
        assert args == ["git", "-C", "/tasks/t1", "submodule", "status", "--recursive"]
        return output

    assert GitClones(run=_status).submodule_status(repo_path="/tasks/t1") == {
        "vendor/lib": SUBMODULE_UNINITIALIZED,
        "vendor/other": SUBMODULE_MODIFIED,
        "vendor/other/nested": SUBMODULE_CURRENT,
        "vendor/conflicted": SUBMODULE_CONFLICTED,
    }


def test_submodule_status_is_empty_without_submodules() -> None:
    assert GitClones(run=lambda *_a, **_kw: "").submodule_status(repo_path="/tasks/t1") == {}


def test_parse_submodule_status_keeps_a_path_with_spaces() -> None:
    # Taking the path as everything between the sha and the ` (describe)` suffix — rather than by
    # field index — is what makes this work.
    assert parse_submodule_status(" abc123 vendor/my lib (heads/main)\n") == {
        "vendor/my lib": SUBMODULE_CURRENT
    }


def test_update_submodules_is_recursive_and_allows_local_transports() -> None:
    rec = _Recorder()
    GitClones(run=rec).update_submodules(repo_path="/tasks/t1")
    # `protocol.file.allow=always` is load-bearing: git ≥2.38 refuses a submodule whose resolved URL
    # is a local path (CVE-2022-39253), which is exactly the local-git flow's `git_url`.
    assert rec.calls[0][0] == [
        "git",
        "-C",
        "/tasks/t1",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
    ]


def test_update_submodules_can_do_one_level_only() -> None:
    # The donor hydration walks the tree a level at a time: a nested submodule's URL can't be
    # resolved (or redirected at the donor) before its parent has been checked out.
    rec = _Recorder()
    GitClones(run=rec).update_submodules(repo_path="/tasks/t1", recursive=False)
    assert "--recursive" not in rec.calls[0][0]


def test_submodule_paths_reads_the_declared_submodules() -> None:
    rec = _Recorder()
    GitClones(run=rec).submodule_paths(repo_path="/tasks/t1")
    # Read from `.gitmodules`, so it answers before `submodule init` and for a submodule that has
    # no checkout yet — and tolerant of a repo that declares none (git exits non-zero).
    argv, check = rec.calls[0]
    assert argv == [
        "git",
        "-C",
        "/tasks/t1",
        "config",
        "--file",
        ".gitmodules",
        "--get-regexp",
        "^submodule\\..*\\.path$",
    ]
    assert check is False


def test_parse_submodule_paths_keeps_dotted_names_and_spaced_paths() -> None:
    output = (
        "submodule.vendor/lib.path vendor/lib\n"
        "submodule.my.lib.path vendor/my lib\n"  # a name with a dot, a path with a space
        "submodule.vendor/lib.url ../lib.git\n"  # not a `.path` line
        "junk\n"
    )
    assert parse_submodule_paths(output) == {
        "vendor/lib": "vendor/lib",
        "my.lib": "vendor/my lib",
    }


def test_init_set_url_and_sync_submodules() -> None:
    rec = _Recorder()
    clones = GitClones(run=rec)
    clones.init_submodules(repo_path="/tasks/t1")
    clones.set_submodule_url(repo_path="/tasks/t1", name="vendor/lib", url="/srv/widget/vendor/lib")
    clones.sync_submodules(repo_path="/tasks/t1")
    assert [argv for argv, _check in rec.calls] == [
        # `init` resolves the declared URLs into config, `config` overrides one with the donor's
        # checkout, and `sync` puts the canonical URLs back afterwards (config *and* each
        # submodule's own origin).
        ["git", "-C", "/tasks/t1", "submodule", "init"],
        ["git", "-C", "/tasks/t1", "config", "submodule.vendor/lib.url", "/srv/widget/vendor/lib"],
        ["git", "-C", "/tasks/t1", "submodule", "sync", "--recursive"],
    ]


# -- a repo's URL: hosted forge vs. a checkout on this host -------------------------


@pytest.mark.parametrize(
    "git_url",
    ["https://github.com/x/y.git", "http://forge/y.git", "ssh://git@forge/y.git", "git@forge:x/y"],
)
def test_forge_urls_have_no_local_path(git_url: str) -> None:
    assert is_forge_url(git_url) is True
    assert local_repo_path(git_url) is None


def test_local_repo_paths_resolve_to_a_filesystem_path() -> None:
    # What makes the host-side push — and cloning a task's submodules out of the repo's own
    # checkout — possible at all.
    assert local_repo_path("/srv/widget") == "/srv/widget"
    assert local_repo_path("file:///srv/widget") == "/srv/widget"
    assert local_repo_path("~/src/widget") == str(Path("~/src/widget").expanduser())
    assert local_repo_path("  ") is None
    assert is_forge_url("C:\\src\\widget") is False  # a drive letter isn't a scp-like remote


def test_push_emits_a_plain_push() -> None:
    rec = _Recorder()
    GitClones(run=rec).push(repo_path="/tasks/t1", remote="origin", branch="main")
    # Never forced, never with --set-upstream: one branch, exactly as it stands.
    assert rec.calls[0][0] == ["git", "-C", "/tasks/t1", "push", "origin", "main"]


def test_push_raises_git_error_carrying_stderr() -> None:
    """A rejected push must surface git's own words — the publisher classifies them into a remedy."""

    def _reject(args: Sequence[str], *, check: bool = True) -> str:
        raise subprocess.CalledProcessError(
            1,
            list(args),
            stderr="! [remote rejected] main -> main (branch is currently checked out)",
        )

    with pytest.raises(GitError) as err:
        GitClones(run=_reject).push(repo_path="/tasks/t1", remote="origin", branch="main")
    assert "currently checked out" in err.value.stderr
    assert "main" in str(err.value)


def test_push_tolerates_a_failure_with_no_stderr() -> None:
    def _reject(args: Sequence[str], *, check: bool = True) -> str:
        raise subprocess.CalledProcessError(1, list(args))  # stderr is None

    with pytest.raises(GitError) as err:
        GitClones(run=_reject).push(repo_path="/tasks/t1", remote="origin", branch="main")
    assert err.value.stderr == ""


# -- integration: a real git repo ---------------------------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_create_and_remove_a_real_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "--initial-branch", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README").write_text("hi")
    run("git", "add", "--all")
    run("git", "commit", "--message", "init")

    git = GitWorktrees()
    wt = git.create(
        repo_path=str(repo),
        worktrees_root=str(tmp_path / "wt"),
        repo_id="r1",
        slug="fix-it",
        base="main",
    )
    assert Path(wt.path).is_dir()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "panopticon/fix-it"],
        capture_output=True,
        text=True,
    ).stdout
    assert "panopticon/fix-it" in branches

    git.remove(repo_path=str(repo), worktree_path=wt.path, force=True)
    assert not Path(wt.path).exists()
