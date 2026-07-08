"""Unit tests for Bootstrap: the build-if-missing decision logic.

A fake CommandRunner records calls and returns pre-configured exit codes, so every
test runs without a real Docker daemon.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from panopticon.terminal.bootstrap import Bootstrap


class _Recorder:
    """Fake CommandRunner: records calls, returns configured exit codes, never touches Docker."""

    def __init__(self, returncodes: list[int] | None = None) -> None:
        self.calls: list[tuple[list[str], bool, bool]] = []  # (args, check, capture_output)
        self._codes = iter(returncodes or [])

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), check, capture_output))
        rc = next(self._codes, 0)
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, args)
        return subprocess.CompletedProcess(args=list(args), returncode=rc, stdout="", stderr="")

    def args_of(self, index: int) -> list[str]:
        return self.calls[index][0]

    def capture_output_of(self, index: int) -> bool:
        return self.calls[index][2]

    def check_of(self, index: int) -> bool:
        return self.calls[index][1]


# -- image_exists -----------------------------------------------------------------

def test_image_exists_returns_true_when_inspect_succeeds() -> None:
    rec = _Recorder(returncodes=[0])
    assert Bootstrap(rec).image_exists("panopticon-base") is True


def test_image_exists_returns_false_when_inspect_fails() -> None:
    rec = _Recorder(returncodes=[1])
    assert Bootstrap(rec).image_exists("panopticon-base") is False


def test_image_exists_does_not_raise_on_nonzero_exit() -> None:
    rec = _Recorder(returncodes=[1])
    Bootstrap(rec).image_exists("panopticon-base")  # must not raise


def test_image_exists_emits_docker_image_inspect_command() -> None:
    rec = _Recorder(returncodes=[0])
    Bootstrap(rec).image_exists("my-image")
    assert rec.args_of(0) == ["docker", "image", "inspect", "my-image"]


def test_image_exists_captures_output_so_inspect_stays_silent() -> None:
    rec = _Recorder(returncodes=[0])
    Bootstrap(rec).image_exists("panopticon-base")
    assert rec.capture_output_of(0) is True


# -- build_image ------------------------------------------------------------------

def test_build_image_emits_docker_build_command() -> None:
    rec = _Recorder()
    Bootstrap(rec).build_image("panopticon-base")
    assert rec.args_of(0) == [
        "docker", "build", "--tag", "panopticon-base", "--file", "docker/Dockerfile", "."
    ]


def test_build_image_does_not_capture_output_so_progress_streams() -> None:
    rec = _Recorder()
    Bootstrap(rec).build_image("panopticon-base")
    assert rec.capture_output_of(0) is False


def test_build_image_respects_custom_dockerfile() -> None:
    rec = _Recorder()
    Bootstrap(rec).build_image("img", dockerfile="path/to/Dockerfile")
    assert rec.args_of(0)[rec.args_of(0).index("--file") + 1] == "path/to/Dockerfile"


# -- ensure_image -----------------------------------------------------------------

def test_ensure_image_returns_false_when_image_already_present() -> None:
    rec = _Recorder(returncodes=[0])  # inspect → present
    assert Bootstrap(rec).ensure_image("panopticon-base") is False


def test_ensure_image_does_not_build_when_image_present() -> None:
    rec = _Recorder(returncodes=[0])
    Bootstrap(rec).ensure_image("panopticon-base")
    assert len(rec.calls) == 1  # only the inspect; no build


def test_ensure_image_returns_true_when_build_triggered() -> None:
    rec = _Recorder(returncodes=[1, 0])  # inspect → absent; build → ok
    assert Bootstrap(rec).ensure_image("panopticon-base") is True


def test_ensure_image_builds_when_image_absent() -> None:
    rec = _Recorder(returncodes=[1, 0])
    Bootstrap(rec).ensure_image("panopticon-base")
    assert len(rec.calls) == 2
    assert "build" in rec.args_of(1)


def test_ensure_image_streams_build_output() -> None:
    rec = _Recorder(returncodes=[1, 0])
    Bootstrap(rec).ensure_image("panopticon-base")
    build_call_index = 1
    assert rec.capture_output_of(build_call_index) is False


def test_ensure_image_raises_when_build_fails() -> None:
    rec = _Recorder(returncodes=[1, 2])  # inspect → absent; build → fails
    with pytest.raises(subprocess.CalledProcessError):
        Bootstrap(rec).ensure_image("panopticon-base")
