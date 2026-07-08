"""Sanity check: documented Makefile targets exist and the Quickstart commands are real."""

from __future__ import annotations

from pathlib import Path

import pytest

_MAKEFILE = Path(__file__).parent.parent / "Makefile"
_README = Path(__file__).parent.parent / "README.md"


@pytest.mark.parametrize("target", ["bootstrap", "demo", "host", "start", "build"])
def test_make_target_exists(target: str) -> None:
    assert f"{target}:" in _MAKEFILE.read_text(), f"Makefile is missing target: {target!r}"


def test_readme_references_make_bootstrap() -> None:
    assert "make bootstrap" in _README.read_text()


def test_readme_references_panopticon_demo() -> None:
    assert "panopticon demo" in _README.read_text()


def test_readme_has_getting_started_section() -> None:
    assert "## Getting started" in _README.read_text()
