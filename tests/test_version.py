"""The version has one source of truth: `__version__` in the package.

`pyproject.toml` declares `dynamic = ["version"]` and hatchling reads it out of
`src/panopticon/__init__.py`, so the distribution metadata is *derived* rather than hand-synced.
These tests hold that line — the two values drifted once before (#363 bumped `pyproject.toml` and
left `__init__.py` behind, #365 repaired it), and `__version__` is the value the runner compares
against a base image's `org.panopticon.version` label, so drift there means a silently stale image.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import panopticon

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_distribution_metadata_matches_dunder_version() -> None:
    # Derived, so this holds by construction — until someone reintroduces a hand-edited literal.
    assert version("panopticon-app") == panopticon.__version__


def test_pyproject_declares_the_version_dynamic() -> None:
    # The guard with teeth: a static `[project] version` would put the value back in two places.
    project = tomllib.loads(_PYPROJECT.read_text())["project"]
    assert "version" not in project, "pyproject.toml must derive the version, not restate it"
    assert "version" in project.get("dynamic", [])
