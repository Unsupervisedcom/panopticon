"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from panopticon.sessionservice.images import ImageBuilder, compose_dockerfile, image_tag


def test_image_tag_names_by_workflow_and_repo() -> None:
    assert image_tag("github-peer-reviewed", "r1") == "panopticon-github-peer-reviewed-r1"


def test_compose_dockerfile_chains_base_then_layers() -> None:
    df = compose_dockerfile("panopticon-base", ["RUN install gh", "", "RUN deps"])
    assert df.startswith("FROM panopticon-base\n")
    assert "RUN install gh" in df and "RUN deps" in df


def test_compose_dockerfile_base_only_when_no_layers() -> None:
    assert compose_dockerfile("base", ["", "  "]) == "FROM base\n"


class _BuildRecorder:
    def __init__(self) -> None:
        self.cmd: list[str] = []
        self.dockerfile = ""

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.cmd = list(args)
        self.dockerfile = (Path(args[-1]) / "Dockerfile").read_text()  # dir exists during the call
        return ""


def test_build_composes_and_runs_docker_build() -> None:
    rec = _BuildRecorder()
    tag = ImageBuilder(base="panopticon-base", run=rec).build("github-peer-reviewed", "r1", ["RUN x"])
    assert tag == "panopticon-github-peer-reviewed-r1"
    assert rec.cmd[:4] == ["docker", "build", "--tag", "panopticon-github-peer-reviewed-r1"]
    assert rec.dockerfile.startswith("FROM panopticon-base") and "RUN x" in rec.dockerfile


class _MultiRecorder:
    """Records all calls and returns canned responses in order (for multi-step sequences)."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.calls.append((list(args), check))
        return self._responses.pop(0) if self._responses else ""


def test_build_base_if_missing_skips_build_when_image_present() -> None:
    rec = _MultiRecorder('[{"Id": "sha256:abc"}]')  # inspect returns JSON → image present
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is False
    assert len(rec.calls) == 1  # only the inspect probe, no build
    assert rec.calls[0][0] == ["docker", "image", "inspect", "panopticon-base"]
    assert rec.calls[0][1] is False  # check=False so a missing image doesn't raise


def test_build_base_if_missing_builds_when_inspect_returns_empty_string() -> None:
    rec = _MultiRecorder("")  # inspect returns "" → image absent
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2
    build_cmd = rec.calls[1][0]
    assert build_cmd == [
        "docker", "build", "--tag", "panopticon-base",
        "--file", "docker/Dockerfile", ".",
    ]
    assert rec.calls[1][1] is True  # check=True so a build failure propagates


def test_build_base_if_missing_builds_when_inspect_returns_empty_array() -> None:
    rec = _MultiRecorder("[]")  # docker inspect outputs "[]" on a missing image
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2  # inspect + build


def test_build_base_if_missing_accepts_custom_context() -> None:
    rec = _MultiRecorder("")  # missing
    ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing(context="/my/project")
    assert rec.calls[1][0][-1] == "/my/project"  # context is the final arg to docker build
