"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawn import cleanup_workspace, prepare_workspace


def _recording_runner(submodule_status: str = "") -> tuple[list[list[str]], Callable[..., str]]:
    """A fake ``git`` that records its commands; ``submodule status`` answers ``submodule_status``.

    The default (empty) is a repo with no submodules — the shape most of these tests want.
    """
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        argv = list(args)  # type: ignore[call-overload]
        calls.append(argv)
        return submodule_status if "submodule" in argv and "status" in argv else ""

    return calls, run


#: One uninitialized submodule, as `git submodule status` reports it (leading `-`).
_UNINITIALIZED = "-8b730cb251d76ed7cd68eb3fd7700e2c23c48c28 vendor/lib\n"

_SUBMODULE_STATUS = ["git", "-C", "/tasks/t1", "submodule", "status", "--recursive"]
_SUBMODULE_UPDATE = [
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


_REPO = {"id": "r1", "git_url": "https://forge/r1.git"}


def test_prepare_clones_the_cache_then_the_per_task_checkout() -> None:
    calls, run = _recording_runner()
    cache = CloneCache(
        "/cache", run=run, exists=lambda _p: False, makedirs=lambda _p: None
    )  # cache absent → clone

    clone = prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: False,
        makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    assert calls == [
        ["git", "clone", "https://forge/r1.git", "/cache/r1"],  # ensure the repo's cache clone…
        [
            "git",
            "clone",
            "--local",
            "/cache/r1",
            "/tasks/t1",
        ],  # …then the self-contained per-task clone
        # …then point origin at the forge (the git_url, verbatim) — not the cache path, which the
        # container can't push to and gh can't resolve (it would fork to the token's own account)
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        _SUBMODULE_STATUS,  # …and check for submodules: none here, so nothing more is emitted
    ]


def test_prepare_is_idempotent_but_still_asserts_origin_when_the_checkout_exists() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    clone = prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
        makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    # checkout already there (e.g. container re-creation) — no clone/fetch, but origin is re-asserted
    # (idempotent set-url), which also repoints a clone left over from before this fix
    assert calls == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        _SUBMODULE_STATUS,
    ]


def test_prepare_uses_the_git_url_verbatim_as_origin() -> None:
    # The git_url is registered in the form the container should use (here SSH); spawn sets it as-is,
    # no rewriting — the URL scheme is the operator's choice at repo setup, not a conversion here.
    calls, run = _recording_runner()
    repo = {"id": "r1", "git_url": "git@github.com:Org/repo.git"}
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    prepare_workspace(
        "t1",
        repo,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
        makedirs=lambda _p: None,
    )

    assert calls == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "git@github.com:Org/repo.git"],
        _SUBMODULE_STATUS,
    ]


def test_prepare_creates_tasks_root_before_cloning(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    assert not tasks_root.exists()
    created: list[str] = []

    cache = CloneCache(str(tmp_path / "cache"), run=lambda *_a, **_kw: "", exists=lambda _p: False)
    prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root=str(tasks_root),
        git=GitClones(run=lambda *_a, **_kw: ""),
        exists=lambda _p: False,
        makedirs=lambda p: (created.append(p), Path(p).mkdir(parents=True, exist_ok=True)),  # type: ignore[func-returns-value]
    )

    assert str(tasks_root) in created
    assert tasks_root.is_dir()


def test_prepare_initializes_submodules_after_repointing_origin() -> None:
    calls, run = _recording_runner(_UNINITIALIZED)
    cache = CloneCache("/cache", run=run, exists=lambda _p: False, makedirs=lambda _p: None)

    prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: False,
        makedirs=lambda _p: None,
    )

    # Order is load-bearing: relative `.gitmodules` URLs (`../lib.git`) resolve against the
    # superproject's origin, so the set-url must already have happened.
    assert calls[-3:] == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        _SUBMODULE_STATUS,
        _SUBMODULE_UPDATE,
    ]


def test_prepare_leaves_already_initialized_submodules_alone() -> None:
    # `+` (submodule checked out at another commit — e.g. the agent moved it) and a plain space
    # both mean initialized. Updating would try to check the recorded commit out over their work.
    for status in (" 8b730cb vendor/lib (heads/main)\n", "+8b730cb vendor/lib (heads/main)\n"):
        calls, run = _recording_runner(status)
        cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

        prepare_workspace(
            "t1",
            _REPO,
            cache=cache,
            tasks_root="/tasks",
            git=GitClones(run=run),
            exists=lambda _p: True,
            makedirs=lambda _p: None,
        )

        assert _SUBMODULE_UPDATE not in calls


def test_prepare_retries_submodules_on_an_existing_checkout() -> None:
    # A submodule fetch that failed transiently leaves the checkout in place, so the clone gate
    # skips it from then on — the status gate is what makes the next spawn pass retry.
    calls, run = _recording_runner(_UNINITIALIZED)
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
        makedirs=lambda _p: None,
    )

    assert calls == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        _SUBMODULE_STATUS,
        _SUBMODULE_UPDATE,
    ]


def test_cleanup_removes_the_checkout_when_it_exists() -> None:
    removed: list[str] = []
    cleanup_workspace("t1", "/tasks", exists=lambda _p: True, rmtree=removed.append)
    assert removed == ["/tasks/t1"]


def test_cleanup_is_a_no_op_when_checkout_is_absent() -> None:
    removed: list[str] = []
    cleanup_workspace("t1", "/tasks", exists=lambda _p: False, rmtree=removed.append)
    assert removed == []


def _raise_permission_denied(_path: str) -> None:
    raise PermissionError(13, "Permission denied", "/tasks/t1/.mypy_cache")


def test_cleanup_quarantines_a_checkout_it_cannot_delete() -> None:
    # A container process that ran as root leaves files the daemon can't delete (e.g. a
    # root-owned .mypy_cache) — rmtree raises. The checkout is renamed aside instead of the
    # error propagating, so the host pass doesn't refail on it every tick.
    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert renamed == [("/tasks/t1", "/tasks/t1.stale")]


def test_cleanup_swallows_a_failed_quarantine() -> None:
    # Even the rename failing (e.g. the quarantine path already exists) must not raise —
    # cleanup is best-effort; it never takes down the host pass.
    def rename_fails(_src: str, _dst: str) -> None:
        raise OSError("target exists")

    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        rename=rename_fails,
    )  # no exception is the assertion


def test_cleanup_uses_docker_to_scrub_root_owned_files() -> None:
    # When rmtree fails (root-owned files), docker_cleanup empties the directory so the
    # second rmtree call can remove the empty dir — no quarantine needed.
    docker_called: list[str] = []
    rmtree_calls = 0

    def rmtree_first_fails_then_succeeds(path: str) -> None:
        nonlocal rmtree_calls
        rmtree_calls += 1
        if rmtree_calls == 1:
            raise PermissionError(13, "Permission denied", "/tasks/t1/.mypy_cache")

    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=rmtree_first_fails_then_succeeds,
        docker_cleanup=docker_called.append,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert docker_called == ["/tasks/t1"]
    assert rmtree_calls == 2  # first fails, second (on the now-empty dir) succeeds
    assert renamed == []  # quarantine not reached


def test_cleanup_quarantines_when_docker_cleanup_also_fails() -> None:
    # When both rmtree and docker_cleanup fail, fall back to quarantine.
    def docker_cleanup_fails(_path: str) -> None:
        raise OSError("docker not available")

    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        docker_cleanup=docker_cleanup_fails,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert renamed == [("/tasks/t1", "/tasks/t1.stale")]


# -- integration: a real repo with a real submodule ---------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_prepare_fills_in_a_real_submodule_that_survives_relocation(tmp_path: Path) -> None:
    """The per-task checkout gets the submodule's content — and keeps it when moved.

    Moving the checkout is the test for the ADR 0011 property submodules could have broken: the
    clone is bind-mounted at ``/workspace``, a different path than the one git saw on the host, so
    the submodule's gitdir/worktree links have to be relative. They are, but only because
    ``submodule update`` writes them that way — worth pinning.
    """

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def init(path: Path) -> None:
        path.mkdir()
        git("init", "--initial-branch", "main", cwd=path)
        git("config", "user.email", "t@example.com", cwd=path)
        git("config", "user.name", "t", cwd=path)

    lib = tmp_path / "lib"  # the submodule's own repo
    init(lib)
    (lib / "L").write_text("libfile")
    git("add", "--all", cwd=lib)
    git("commit", "--message", "init", cwd=lib)

    forge = tmp_path / "forge"  # the repo a task works on, with `../lib` as a submodule
    init(forge)
    (forge / "T").write_text("top")
    git("add", "--all", cwd=forge)
    git("commit", "--message", "init", cwd=forge)
    # A *relative* submodule URL — the common case, and the one that only resolves correctly
    # because prepare_workspace repoints origin at the forge before initializing submodules.
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "../lib",
        "vendor/lib",
        cwd=forge,
    )
    git("commit", "--message", "add submodule", cwd=forge)

    cache = CloneCache(str(tmp_path / "cache"))
    clone = Path(
        prepare_workspace(
            "t1",
            {"id": "r1", "git_url": str(forge)},
            cache=cache,
            tasks_root=str(tmp_path / "tasks"),
        )
    )

    assert (clone / "vendor" / "lib" / "L").read_text() == "libfile"  # not an empty directory

    moved = clone.parent / "moved"  # stand-in for the /workspace bind mount
    clone.rename(moved)
    subprocess.run(
        ["git", "-C", str(moved / "vendor" / "lib"), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    )  # the submodule is still a working repo at its new path
