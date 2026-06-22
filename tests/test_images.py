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

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.cmd = list(args)
        self.dockerfile = (Path(args[-1]) / "Dockerfile").read_text()  # dir exists during the call
        return ""


def test_build_composes_and_runs_docker_build() -> None:
    rec = _BuildRecorder()
    tag = ImageBuilder(base="panopticon-base", run=rec).build("github-peer-reviewed", "r1", ["RUN x"])
    assert tag == "panopticon-github-peer-reviewed-r1"
    assert rec.cmd[:4] == ["docker", "build", "--tag", "panopticon-github-peer-reviewed-r1"]
    assert rec.dockerfile.startswith("FROM panopticon-base") and "RUN x" in rec.dockerfile
