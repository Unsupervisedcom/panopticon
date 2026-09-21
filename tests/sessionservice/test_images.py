"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

import importlib.resources
from collections.abc import Sequence
from pathlib import Path

import panopticon
import panopticon.docker as _docker_pkg
from panopticon.sessionservice.images import (
    VERSION_LABEL,
    ImageBuilder,
    compose_dockerfile,
    image_tag,
)


def _assert_version_stamp(build_cmd: list[str]) -> None:
    """The base build stamps the package version both as a build-arg and the staleness label."""
    assert "--build-arg" in build_cmd
    assert (
        build_cmd[build_cmd.index("--build-arg") + 1]
        == f"PANOPTICON_VERSION={panopticon.__version__}"
    )
    assert "--label" in build_cmd
    assert build_cmd[build_cmd.index("--label") + 1] == f"{VERSION_LABEL}={panopticon.__version__}"


def _bundled_dockerfile() -> str:
    return (importlib.resources.files(_docker_pkg) / "Dockerfile").read_text()


def test_image_tag_names_by_cli_workflow_and_repo() -> None:
    # Default CLI (claude) and an explicit non-default one — the CLI dimension keeps a repo built
    # for two CLIs from colliding (ADR 0014 §4).
    assert image_tag("github-peer-reviewed", "r1") == "panopticon-claude-github-peer-reviewed-r1"
    assert (
        image_tag("github-peer-reviewed", "r1", "codex")
        == "panopticon-codex-github-peer-reviewed-r1"
    )


def test_compose_dockerfile_chains_base_then_layers() -> None:
    df = compose_dockerfile("panopticon-base", ["RUN install gh", "", "RUN deps"])
    assert df.startswith("FROM panopticon-base\n")
    assert "RUN install gh" in df and "RUN deps" in df


def test_compose_dockerfile_base_only_when_no_layers() -> None:
    assert compose_dockerfile("base", ["", "  "]) == "FROM base\n"


def test_bundled_dockerfile_selects_the_cli_via_the_agent_cli_arg() -> None:
    df = _bundled_dockerfile()
    # The build arg the runner/Makefile pass to pick the CLI (ADR 0014 §4), defaulting to claude so
    # an un-parameterized build stays the claude base.
    assert "ARG AGENT_CLI=claude" in df
    # Each CLI's install is gated on the arg, so only the selected runtime lands in the variant.
    assert 'if [ "$AGENT_CLI" = "claude" ]' in df
    assert 'if [ "$AGENT_CLI" = "codex" ]' in df


def test_bundled_dockerfile_installs_the_pinned_codex_musl_binary() -> None:
    df = _bundled_dockerfile()
    # The codex release is pinned via a build arg (the single place to bump it); the install pulls
    # the statically-linked musl binary straight from GitHub releases — no node/npm.
    assert "ARG CODEX_VERSION=0.144.4" in df
    assert (
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-$triple.tar.gz"
        in df
    )
    # Both linux musl arches are handled and the binary lands world-executable on PATH.
    assert "x86_64-unknown-linux-musl" in df and "aarch64-unknown-linux-musl" in df
    assert "chmod 0755 /usr/local/bin/codex" in df


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
    tag = ImageBuilder(run=rec).build("github-peer-reviewed", "r1", ["RUN x"])
    assert tag == "panopticon-claude-github-peer-reviewed-r1"
    assert rec.cmd[:4] == ["docker", "build", "--tag", "panopticon-claude-github-peer-reviewed-r1"]
    # The composed image FROMs the resolved CLI's base variant (ADR 0014 §4), not a bare base.
    assert rec.dockerfile.startswith("FROM panopticon-base-claude") and "RUN x" in rec.dockerfile


def test_build_composes_onto_the_selected_cli_base_variant() -> None:
    rec = _BuildRecorder()
    tag = ImageBuilder(run=rec).build("wf", "r1", ["RUN x"], agent_cli="codex")
    assert tag == "panopticon-codex-wf-r1"
    assert rec.dockerfile.startswith("FROM panopticon-base-codex")


class _MultiRecorder:
    """Records all calls and returns canned responses in order (for multi-step sequences)."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True, verbose: bool = False) -> str:
        self.calls.append((list(args), check))
        return self._responses.pop(0) if self._responses else ""


def test_build_base_if_missing_skips_build_when_version_label_matches() -> None:
    # The inspect --format reads back the version stamp; matching the installed package → current,
    # so the hot path is a single inspect with no rebuild.
    rec = _MultiRecorder(f"{panopticon.__version__}\n")
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is False
    assert len(rec.calls) == 1  # only the inspect probe, no build
    inspect_cmd = rec.calls[0][0]
    assert inspect_cmd[:3] == ["docker", "image", "inspect"]
    assert "--format" in inspect_cmd
    assert VERSION_LABEL in inspect_cmd[inspect_cmd.index("--format") + 1]
    assert inspect_cmd[-1] == "panopticon-base"
    assert rec.calls[0][1] is False  # check=False so a missing image doesn't raise


def test_build_base_if_missing_rebuilds_when_image_absent() -> None:
    rec = _MultiRecorder("")  # inspect on a missing image → empty stamp
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2  # inspect + build
    build_cmd = rec.calls[1][0]
    # command structure: docker build --tag <img> --label … --build-arg PANOPTICON_VERSION=<v>
    #                    --build-arg AGENT_CLI=<cli> --file <path> <dir>
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    _assert_version_stamp(build_cmd)
    # No agent_cli given → the claude default selects the claude base variant's install (ADR 0014 §4).
    assert "AGENT_CLI=claude" in build_cmd
    assert "--file" in build_cmd
    assert build_cmd[build_cmd.index("--file") + 1].endswith("Dockerfile")
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile
    assert rec.calls[1][1] is True  # check=True so a build failure propagates


def test_build_base_if_missing_selects_the_codex_cli_install() -> None:
    rec = _MultiRecorder("")  # inspect returns "" (absent) → build
    result = ImageBuilder(run=rec).build_base_if_missing(agent_cli="codex")
    assert result is True
    inspect_cmd, build_cmd = rec.calls[0][0], rec.calls[1][0]
    assert inspect_cmd[:3] == ["docker", "image", "inspect"]
    assert inspect_cmd[-1] == "panopticon-base-codex"
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base-codex"]
    # The AGENT_CLI build arg drives which CLI the bundled Dockerfile installs (ADR 0014 §4).
    assert "AGENT_CLI=codex" in build_cmd


def test_build_base_if_missing_rebuilds_when_version_label_stale() -> None:
    # An older stamp (the reported bug: the baked package lagging the installed one) → rebuild.
    rec = _MultiRecorder("0.0.1\n")
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2  # inspect + build
    assert rec.calls[1][0][:4] == ["docker", "build", "--tag", "panopticon-base"]
    _assert_version_stamp(rec.calls[1][0])


def test_build_base_unconditional() -> None:
    rec = _MultiRecorder("")
    ImageBuilder(base="panopticon-base", run=rec).build_base(verbose=True)
    assert len(rec.calls) == 1  # no inspect probe — just the build
    build_cmd = rec.calls[0][0]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    _assert_version_stamp(build_cmd)
    assert "AGENT_CLI=claude" in build_cmd  # default CLI when none is given
    assert "--file" in build_cmd
    assert build_cmd[build_cmd.index("--file") + 1].endswith("Dockerfile")
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile


def test_build_base_tags_and_installs_the_codex_variant() -> None:
    rec = _MultiRecorder("")
    ImageBuilder(run=rec).build_base(agent_cli="codex")
    build_cmd = rec.calls[0][0]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base-codex"]
    assert "AGENT_CLI=codex" in build_cmd
