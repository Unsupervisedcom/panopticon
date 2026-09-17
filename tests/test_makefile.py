"""Regression tests for developer commands emitted by the Makefile."""

from __future__ import annotations

import subprocess


def test_build_stamps_base_image_with_panopticon_version() -> None:
    result = subprocess.run(
        [
            "make",
            "--dry-run",
            "build",
            "AGENT_CLIS=claude",
            "PANOPTICON_VERSION=9.8.7",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "docker build" in result.stdout
    assert "--tag panopticon-base-$cli" in result.stdout
    assert "--label org.panopticon.version=9.8.7" in result.stdout


def test_restart_wraps_the_cli_command() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "restart"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "panopticon restart" in result.stdout
