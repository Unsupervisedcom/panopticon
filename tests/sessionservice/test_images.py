"""Composable task images (ADR 0005): tag naming, Dockerfile composition, and the build
command — unit-tested without a real daemon (the command-runner is faked)."""

from __future__ import annotations

import importlib.resources
from collections.abc import Sequence
from pathlib import Path

import panopticon.docker as _docker_pkg
from panopticon.sessionservice.images import ImageBuilder, compose_dockerfile, image_tag


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
    # command structure: docker build --tag <img> --build-arg PANOPTICON_VERSION=<v>
    #                    --build-arg AGENT_CLI=<cli> --file <path> <dir>
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert "--build-arg" in build_cmd
    version_arg = build_cmd[build_cmd.index("--build-arg") + 1]
    assert version_arg.startswith("PANOPTICON_VERSION=")
    # No agent_cli given → the claude default selects the claude base variant's install (ADR 0014 §4).
    assert "AGENT_CLI=claude" in build_cmd
    assert "--file" in build_cmd
    file_arg = build_cmd[build_cmd.index("--file") + 1]
    assert file_arg.endswith("Dockerfile")
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile
    assert rec.calls[1][1] is True  # check=True so a build failure propagates


def test_build_base_if_missing_selects_the_codex_cli_install() -> None:
    rec = _MultiRecorder("")  # inspect returns "" → image absent
    result = ImageBuilder(run=rec).build_base_if_missing(agent_cli="codex")
    assert result is True
    inspect_cmd, build_cmd = rec.calls[0][0], rec.calls[1][0]
    assert inspect_cmd == ["docker", "image", "inspect", "panopticon-base-codex"]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base-codex"]
    # The AGENT_CLI build arg drives which CLI the bundled Dockerfile installs (ADR 0014 §4).
    assert "AGENT_CLI=codex" in build_cmd


def test_build_base_if_missing_builds_when_inspect_returns_empty_array() -> None:
    rec = _MultiRecorder("[]")  # docker inspect outputs "[]" on a missing image
    result = ImageBuilder(base="panopticon-base", run=rec).build_base_if_missing()
    assert result is True
    assert len(rec.calls) == 2  # inspect + build


def test_build_base_unconditional() -> None:
    rec = _MultiRecorder("")
    ImageBuilder(base="panopticon-base", run=rec).build_base(verbose=True)
    assert len(rec.calls) == 1  # no inspect probe — just the build
    build_cmd = rec.calls[0][0]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base"]
    assert "--build-arg" in build_cmd
    version_arg = build_cmd[build_cmd.index("--build-arg") + 1]
    assert version_arg.startswith("PANOPTICON_VERSION=")
    assert "AGENT_CLI=claude" in build_cmd  # default CLI when none is given
    assert "--file" in build_cmd
    file_arg = build_cmd[build_cmd.index("--file") + 1]
    assert file_arg.endswith("Dockerfile")
    assert Path(build_cmd[-1]).name == "docker"  # context = parent dir of Dockerfile


def test_build_base_tags_and_installs_the_codex_variant() -> None:
    rec = _MultiRecorder("")
    ImageBuilder(run=rec).build_base(agent_cli="codex")
    build_cmd = rec.calls[0][0]
    assert build_cmd[:4] == ["docker", "build", "--tag", "panopticon-base-codex"]
    assert "AGENT_CLI=codex" in build_cmd
