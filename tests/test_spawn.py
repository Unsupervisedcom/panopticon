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
    ]


def test_prepare_is_idempotent_when_the_checkout_exists() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: True)

    clone = prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks", git=GitClones(run=run), exists=lambda _p: True
    )

    assert clone == "/tasks/t1"
    assert calls == []  # checkout already there (e.g. container re-creation) — no git, no cache fetch
