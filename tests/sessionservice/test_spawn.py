"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawn import cleanup_workspace, prepare_workspace


def _recording_runner() -> tuple[list[list[str]], Callable[..., str]]:
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return ""

    return calls, run


_REPO = {"id": "r1", "git_url": "https://forge/r1.git"}


def test_prepare_clones_the_cache_then_the_per_task_checkout() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: False, makedirs=lambda _p: None)  # cache absent → clone

    clone = prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks", git=GitClones(run=run),
        exists=lambda _p: False, makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    assert calls == [
        ["git", "clone", "https://forge/r1.git", "/cache/r1"],  # ensure the repo's cache clone…
        ["git", "clone", "--local", "/cache/r1", "/tasks/t1"],  # …then the self-contained per-task clone
        # …then point origin at the forge (the git_url, verbatim) — not the cache path, which the
        # container can't push to and gh can't resolve (it would fork to the token's own account)
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
    ]


def test_prepare_is_idempotent_but_still_asserts_origin_when_the_checkout_exists() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    clone = prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks", git=GitClones(run=run),
        exists=lambda _p: True, makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    # checkout already there (e.g. container re-creation) — no clone/fetch, but origin is re-asserted
    # (idempotent set-url), which also repoints a clone left over from before this fix
    assert calls == [["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"]]


def test_prepare_uses_the_git_url_verbatim_as_origin() -> None:
    # The git_url is registered in the form the container should use (here SSH); spawn sets it as-is,
    # no rewriting — the URL scheme is the operator's choice at repo setup, not a conversion here.
    calls, run = _recording_runner()
    repo = {"id": "r1", "git_url": "git@github.com:Org/repo.git"}
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    prepare_workspace(
        "t1", repo, cache=cache, tasks_root="/tasks", git=GitClones(run=run),
        exists=lambda _p: True, makedirs=lambda _p: None,
    )

    assert calls == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "git@github.com:Org/repo.git"]
    ]


def test_prepare_creates_tasks_root_before_cloning(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    assert not tasks_root.exists()
    created: list[str] = []

    cache = CloneCache(str(tmp_path / "cache"), run=lambda *_a, **_kw: "", exists=lambda _p: False)
    prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root=str(tasks_root),
        git=GitClones(run=lambda *_a, **_kw: ""),
        exists=lambda _p: False,
        makedirs=lambda p: (created.append(p), Path(p).mkdir(parents=True, exist_ok=True)),  # type: ignore[func-returns-value]
    )

    assert str(tasks_root) in created
    assert tasks_root.is_dir()


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
        "t1", "/tasks",
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
        "t1", "/tasks",
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
        "t1", "/tasks",
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
        "t1", "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        docker_cleanup=docker_cleanup_fails,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert renamed == [("/tasks/t1", "/tasks/t1.stale")]
