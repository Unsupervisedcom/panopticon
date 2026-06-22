"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

from collections.abc import Callable

from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawn import prepare_workspace


def _recording_runner() -> tuple[list[list[str]], Callable[..., str]]:
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return ""

    return calls, run


_REPO = {"id": "r1", "git_url": "https://forge/r1.git"}


def test_prepare_clones_the_cache_then_the_per_task_checkout() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: False)  # cache absent → clone

    clone = prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks", git=GitClones(run=run), exists=lambda _p: False
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
    cache = CloneCache("/cache", run=run, exists=lambda _p: True)

    clone = prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks", git=GitClones(run=run), exists=lambda _p: True
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
    cache = CloneCache("/cache", run=run, exists=lambda _p: True)

    prepare_workspace(
        "t1", repo, cache=cache, tasks_root="/tasks", git=GitClones(run=run), exists=lambda _p: True
    )

    assert calls == [
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "git@github.com:Org/repo.git"]
    ]
