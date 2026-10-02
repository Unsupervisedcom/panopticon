"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
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


#: A local ``git_url``: the repo's own checkout on this host, which spawn-prep can clone the
#: submodules out of instead of fetching them (the donor).
_LOCAL_REPO = {"id": "r1", "git_url": "/srv/widget"}

_GITMODULES_REGEXP = ["config", "--file", ".gitmodules", "--get-regexp", "^submodule\\..*\\.path$"]


def _gitmodules(repo_path: str) -> list[str]:
    return ["git", "-C", repo_path, *_GITMODULES_REGEXP]


def _hydration_runner(
    *,
    declared: Mapping[str, str],
    statuses: Sequence[str],
    fails: Callable[[list[str]], bool] = lambda _argv: False,
) -> tuple[list[list[str]], Callable[..., str]]:
    """A fake ``git`` for the donor-hydration tests.

    ``declared`` maps a repo path to its ``.gitmodules`` ``--get-regexp`` output (so a nested
    superproject can declare its own submodules); ``statuses`` answers successive
    ``submodule status`` calls, the last one repeating; ``fails`` picks commands that blow up.
    """
    calls: list[list[str]] = []
    remaining = list(statuses)

    def run(args: object, *, check: bool = True) -> str:
        argv = list(args)  # type: ignore[call-overload]
        calls.append(argv)
        if fails(argv):
            raise subprocess.CalledProcessError(1, argv)
        repo_path = argv[2] if len(argv) > 2 and argv[1] == "-C" else ""
        if "status" in argv:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if "--get-regexp" in argv:
            return declared.get(repo_path, "")
        return ""

    return calls, run


def _hydrate(
    *,
    declared: Mapping[str, str],
    statuses: Sequence[str],
    fails: Callable[[list[str]], bool] = lambda _argv: False,
    exists: Callable[[str], bool] = lambda _p: True,
) -> list[list[str]]:
    """Run ``prepare_workspace`` for a repo with a local checkout; return the emitted commands."""
    calls, run = _hydration_runner(declared=declared, statuses=statuses, fails=fails)
    cache = CloneCache("/cache", run=run, exists=exists, makedirs=lambda _p: None)
    prepare_workspace(
        "t1",
        _LOCAL_REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=exists,
        makedirs=lambda _p: None,
    )
    return calls


def test_prepare_clones_submodules_from_the_repos_own_checkout() -> None:
    # The whole point: each submodule is a *local* clone off the repo's checkout on this host
    # (hardlinked objects, no network), not a fetch from its forge paid once per task.
    calls = _hydrate(
        declared={"/tasks/t1": "submodule.vendor/lib.path vendor/lib\n"},
        statuses=[_UNINITIALIZED, ""],  # uninitialized, then hydrated
    )

    assert calls[calls.index(_SUBMODULE_STATUS) :] == [
        _SUBMODULE_STATUS,
        _gitmodules("/tasks/t1"),
        ["git", "-C", "/tasks/t1", "submodule", "init"],
        # …the resolved URL overridden with the donor's checkout of that submodule…
        ["git", "-C", "/tasks/t1", "config", "submodule.vendor/lib.url", "/srv/widget/vendor/lib"],
        # …and updated one level at a time (a nested submodule can't be resolved before its parent).
        [
            "git",
            "-C",
            "/tasks/t1",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
        ],
        _gitmodules("/tasks/t1/vendor/lib"),  # nothing nested below it
        # `sync` puts the canonical URLs back, in the config and in each submodule's own origin.
        ["git", "-C", "/tasks/t1", "submodule", "sync", "--recursive"],
        _SUBMODULE_STATUS,  # …and the result is checked, which is what gates the fallback
    ]
    assert _SUBMODULE_UPDATE not in calls  # never fetched them


def test_prepare_hydrates_nested_submodules_from_the_matching_donor_level() -> None:
    calls = _hydrate(
        declared={
            "/tasks/t1": "submodule.vendor/lib.path vendor/lib\n",
            "/tasks/t1/vendor/lib": "submodule.nested.path nested\n",
        },
        statuses=[_UNINITIALIZED, ""],
    )

    # The donor is matched by *path*, level by level — never by URL, which the donor and the
    # per-task clone resolve against different origins.
    assert ["git", "-C", "/tasks/t1/vendor/lib", "submodule", "init"] in calls
    assert [
        "git",
        "-C",
        "/tasks/t1/vendor/lib",
        "config",
        "submodule.nested.url",
        "/srv/widget/vendor/lib/nested",
    ] in calls
    assert calls.count(["git", "-C", "/tasks/t1", "submodule", "sync", "--recursive"]) == 1


def test_prepare_leaves_a_submodule_the_donor_lacks_pointing_at_its_own_url() -> None:
    # The donor is an optimisation per submodule, not a precondition: one it hasn't checked out
    # keeps the URL git resolved and is fetched as before.
    calls = _hydrate(
        declared={"/tasks/t1": "submodule.vendor/lib.path vendor/lib\n"},
        statuses=[_UNINITIALIZED, ""],
        exists=lambda p: p != "/srv/widget/vendor/lib",
    )

    assert not [c for c in calls if c[3:4] == ["config"] and "submodule.vendor/lib.url" in c]
    assert ["git", "-C", "/tasks/t1", "submodule", "init"] in calls


def test_prepare_fetches_submodules_when_the_repo_has_no_checkout_on_this_host() -> None:
    # A hosted-forge `git_url` has no donor: exactly the previous behaviour, one recursive update.
    calls, run = _hydration_runner(declared={}, statuses=[_UNINITIALIZED])
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

    assert calls[-1] == _SUBMODULE_UPDATE
    assert not [c for c in calls if "--get-regexp" in c]


def test_prepare_fetches_submodules_when_the_donor_checkout_is_gone() -> None:
    calls = _hydrate(
        declared={"/tasks/t1": "submodule.vendor/lib.path vendor/lib\n"},
        statuses=[_UNINITIALIZED],
        exists=lambda p: p != "/srv/widget",  # the registered repo path isn't there any more
    )

    assert calls[-1] == _SUBMODULE_UPDATE


def test_prepare_fetches_submodules_when_hydrating_from_the_donor_fails() -> None:
    calls = _hydrate(
        declared={"/tasks/t1": "submodule.vendor/lib.path vendor/lib\n"},
        statuses=[_UNINITIALIZED],
        fails=lambda argv: argv[3:] == ["submodule", "init"],
    )

    assert calls[-1] == _SUBMODULE_UPDATE  # a broken donor is never worse than no donor


def test_prepare_fetches_submodules_when_hydration_leaves_one_uninitialized() -> None:
    # E.g. the donor is behind and lacks the commit the superproject records: the status gate
    # catches it and the plain update fetches what's missing.
    calls = _hydrate(
        declared={"/tasks/t1": "submodule.vendor/lib.path vendor/lib\n"},
        statuses=[_UNINITIALIZED],
    )

    assert calls[-1] == _SUBMODULE_UPDATE


# -- integration: a real repo with a real submodule ---------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git("init", "--initial-branch", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)


def _add_submodule(superproject: Path, url: str, path: str) -> None:
    _git("-c", "protocol.file.allow=always", "submodule", "add", url, path, cwd=superproject)
    _git("commit", "--message", f"add {path}", cwd=superproject)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_prepare_fills_in_a_real_submodule_that_survives_relocation(tmp_path: Path) -> None:
    """The per-task checkout gets the submodule's content — and keeps it when moved.

    Moving the checkout is the test for the ADR 0011 property submodules could have broken: the
    clone is bind-mounted at ``/workspace``, a different path than the one git saw on the host, so
    the submodule's gitdir/worktree links have to be relative. They are, but only because
    ``submodule update`` writes them that way — worth pinning.
    """
    git, init = _git, _init_repo

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
    _add_submodule(forge, "../lib", "vendor/lib")

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


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_prepare_hardlinks_real_submodules_out_of_the_source_repo(tmp_path: Path) -> None:
    """Submodules — nested ones included — are cloned out of the repo's own checkout on this host.

    The two properties that make this worth doing at all: the objects are **hardlinked** from the
    source repo rather than fetched (that's the cost saving), and once hydration is done nothing
    in the checkout points at the donor any more (``submodule sync`` restores the canonical URLs),
    so the container gets a repo it can fetch and push normally.
    """
    inner = tmp_path / "inner"  # a submodule of the submodule
    _init_repo(inner)
    (inner / "I").write_text("innerfile")
    _git("add", "--all", cwd=inner)
    _git("commit", "--message", "init", cwd=inner)

    lib = tmp_path / "lib"
    _init_repo(lib)
    (lib / "L").write_text("libfile")
    _git("add", "--all", cwd=lib)
    _git("commit", "--message", "init", cwd=lib)
    _add_submodule(lib, str(inner), "nested")

    source = tmp_path / "source"  # the repo's checkout on this host — registered as its git_url
    _init_repo(source)
    (source / "T").write_text("top")
    _git("add", "--all", cwd=source)
    _git("commit", "--message", "init", cwd=source)
    _add_submodule(source, "../lib", "vendor/lib")
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
        cwd=source,
    )

    # Move the submodules' own repos out of the way: the *only* copy of their objects left on
    # this host is the source repo's, so a task that filled its submodules in the old way — by
    # fetching each one from its URL — would now fail outright. Hydration has to find them.
    lib.rename(tmp_path / "lib.gone")
    inner.rename(tmp_path / "inner.gone")

    clone = Path(
        prepare_workspace(
            "t1",
            {"id": "r1", "git_url": str(source)},
            cache=CloneCache(str(tmp_path / "cache")),
            tasks_root=str(tmp_path / "tasks"),
        )
    )

    assert (clone / "vendor" / "lib" / "L").read_text() == "libfile"
    assert (clone / "vendor" / "lib" / "nested" / "I").read_text() == "innerfile"

    # Cheap: the submodules' object stores are the source repo's, hardlinked — no bytes copied
    # and no fetch. (Same filesystem here; a cross-filesystem donor degrades to a copy.)
    for module in ("vendor/lib", "vendor/lib/modules/nested"):
        objects = clone / ".git" / "modules" / module / "objects"
        shared = [
            obj
            for obj in objects.rglob("*")
            if obj.is_file()
            and obj.stat().st_nlink > 1
            and obj.stat().st_ino
            == (source / ".git" / "modules" / module / "objects" / obj.relative_to(objects))
            .stat()
            .st_ino
        ]
        assert shared, f"{module} objects were copied or fetched, not hardlinked"

    # …and nothing points at the donor afterwards: the canonical URLs are back in the config and
    # in each submodule's own origin, so the container fetches and pushes where it should.
    config = _git("config", "--get-regexp", "^submodule\\.", cwd=clone)
    assert str(source / "vendor") not in config
    assert _git("config", "remote.origin.url", cwd=clone / "vendor" / "lib").strip() == str(
        tmp_path / "lib"
    )
    assert _git("config", "remote.origin.url", cwd=clone / "vendor" / "lib" / "nested").strip() == (
        str(inner)
    )
