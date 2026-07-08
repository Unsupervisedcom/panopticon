"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawn import _parse_env_file, prepare_workspace


def _recording_runner() -> tuple[list[list[str]], Callable[..., str]]:
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True, env: object = None) -> str:
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


# -- env file passthrough -----------------------------------------------------------


def test_prepare_passes_env_from_env_file_to_cache_ensure() -> None:
    """Parsed env from repo's env_file is forwarded to cache.ensure() for private-repo auth."""
    received_envs: list[dict[str, str] | None] = []

    class _EnvRecorder:
        def __call__(self, args: object, *, check: bool = True, env: dict[str, str] | None = None) -> str:
            received_envs.append(env)
            return ""

    rec = _EnvRecorder()
    cache = CloneCache("/cache", run=rec, exists=lambda _p: False, makedirs=lambda _p: None)
    repo = {**_REPO, "env_file": "/secrets/repo.env"}

    prepare_workspace(
        "t1", repo, cache=cache, tasks_root="/tasks",
        git=GitClones(run=lambda *_a, **_kw: ""),
        exists=lambda _p: False, makedirs=lambda _p: None,
        parse_env=lambda _path: {"GH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk"},
    )

    # cache.ensure received the parsed env
    assert received_envs[0] == {"GH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk"}


def test_prepare_passes_no_env_when_repo_has_no_env_file() -> None:
    received_envs: list[dict[str, str] | None] = []

    class _EnvRecorder:
        def __call__(self, args: object, *, check: bool = True, env: dict[str, str] | None = None) -> str:
            received_envs.append(env)
            return ""

    rec = _EnvRecorder()
    cache = CloneCache("/cache", run=rec, exists=lambda _p: False, makedirs=lambda _p: None)

    prepare_workspace(
        "t1", _REPO, cache=cache, tasks_root="/tasks",
        git=GitClones(run=lambda *_a, **_kw: ""),
        exists=lambda _p: False, makedirs=lambda _p: None,
    )

    assert received_envs[0] is None


def test_parse_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "repo.env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "GH_TOKEN=ghp_abc\n"
        "ANTHROPIC_API_KEY=sk-ant\n"
        "URL=https://example.com/path?a=1\n"  # value with embedded '='
    )
    result = _parse_env_file(str(env_file))
    assert result == {
        "GH_TOKEN": "ghp_abc",
        "ANTHROPIC_API_KEY": "sk-ant",
        "URL": "https://example.com/path?a=1",
    }
