"""task_session_paths: unit tests pin the emitted docker commands via a fake runner; one
integration test exercises a real docker volume (skipped when docker is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.transcripts import task_session_paths


class _FakeRunner:
    """An injectable CommandRunner: records every call and answers from a canned script."""

    def __init__(
        self,
        *,
        volume_exists: bool = True,
        listing: str = "",
        files: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self._volume_exists = volume_exists
        self._listing = listing
        self._files = files or {}

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        interactive: bool = False,
        verbose: bool = False,
    ) -> str:
        args = list(args)
        self.calls.append((args, check))
        if args[:3] == ["docker", "volume", "inspect"]:
            if not self._volume_exists:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if "find" in args:
            return self._listing
        if "cat" in args:
            path = args[-1]
            return self._files.get(Path(path).name, "")
        return ""


def test_checks_volume_exists_before_anything_else(tmp_path: Path) -> None:
    rec = _FakeRunner(volume_exists=True, listing="a.jsonl\n", files={"a.jsonl": '{"x": 1}\n'})
    paths = task_session_paths("t1", dest=tmp_path, run=rec)
    inspect_call, _ = rec.calls[0]
    assert inspect_call == ["docker", "volume", "inspect", "panopticon-config-t1"]
    assert [p.name for p in paths] == ["a.jsonl"]
    assert (tmp_path / "a.jsonl").read_text() == '{"x": 1}\n'


def test_missing_volume_returns_empty_without_a_docker_run(tmp_path: Path) -> None:
    rec = _FakeRunner(volume_exists=False)
    assert task_session_paths("nope", dest=tmp_path, run=rec) == []
    # Only the inspect call — a bare `docker run --volume <missing>` would silently create the
    # volume, so we must never reach that call when inspect fails.
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == ["docker", "volume", "inspect", "panopticon-config-nope"]


def test_no_docker_binary_returns_empty(tmp_path: Path) -> None:
    def raising_run(args: Sequence[str], *, check: bool = True, **kwargs: object) -> str:
        raise FileNotFoundError("docker")

    assert task_session_paths("t1", dest=tmp_path, run=raising_run) == []


def test_finds_and_reads_every_session_file(tmp_path: Path) -> None:
    rec = _FakeRunner(
        volume_exists=True,
        listing="session-b.jsonl\nsession-a.jsonl\n",
        files={"session-a.jsonl": "a\n", "session-b.jsonl": "b\n"},
    )
    paths = task_session_paths("t1", dest=tmp_path, run=rec)
    contents = {p.name: p.read_text() for p in paths}
    assert contents == {"session-a.jsonl": "a\n", "session-b.jsonl": "b\n"}


def test_emits_the_expected_docker_run_argv(tmp_path: Path) -> None:
    rec = _FakeRunner(volume_exists=True, listing="a.jsonl\n", files={"a.jsonl": "x\n"})
    task_session_paths("t1", dest=tmp_path, run=rec, image="my-image")
    find_call, find_check = next(c for c in rec.calls if "find" in c[0])
    assert find_call == [
        "docker",
        "run",
        "--rm",
        "--volume",
        "panopticon-config-t1:/home/panopticon/.claude:ro",
        "my-image",
        "find",
        "/home/panopticon/.claude/projects/-workspace",
        "-maxdepth",
        "1",
        "-name",
        "*.jsonl",
    ]
    assert find_check is False  # tolerate an existing-but-empty volume (no such directory)
    cat_call, _ = next(c for c in rec.calls if "cat" in c[0])
    assert cat_call[-2:] == ["cat", "/home/panopticon/.claude/projects/-workspace/a.jsonl"]


def test_empty_listing_returns_no_paths(tmp_path: Path) -> None:
    rec = _FakeRunner(volume_exists=True, listing="")
    assert task_session_paths("t1", dest=tmp_path, run=rec) == []


def test_default_dest_is_a_fresh_tempdir() -> None:
    rec = _FakeRunner(volume_exists=True, listing="a.jsonl\n", files={"a.jsonl": "x\n"})
    paths = task_session_paths("t1", run=rec)
    assert paths[0].exists()
    assert paths[0].read_text() == "x\n"


# -- integration: a real docker volume ----------------------------------------------

_HAVE_DOCKER = bool(shutil.which("docker"))


def _docker_running() -> bool:
    return _HAVE_DOCKER and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_running(), reason="needs a working docker daemon")
def test_real_volume_round_trip(tmp_path: Path) -> None:
    volume = "panopticon-config-itest-transcripts"
    subprocess.run(["docker", "volume", "rm", "--force", volume], capture_output=True)
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        # Write a fixture transcript into the volume via a throwaway container — mirroring where
        # the real claude session would have written it.
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{volume}:/home/panopticon/.claude",
                "alpine",
                "sh",
                "-c",
                "mkdir -p /home/panopticon/.claude/projects/-workspace && "
                'echo \'{"type": "user"}\' > '
                "/home/panopticon/.claude/projects/-workspace/session-1.jsonl",
            ],
            check=True,
            capture_output=True,
        )
        paths = task_session_paths("itest-transcripts", dest=tmp_path, image="alpine")
        assert [p.name for p in paths] == ["session-1.jsonl"]
        assert paths[0].read_text().strip() == '{"type": "user"}'
    finally:
        subprocess.run(["docker", "volume", "rm", "--force", volume], capture_output=True)
