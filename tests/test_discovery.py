"""Workflow discovery (Slice 8): the built-in package + an optional path are scanned for concrete
`Workflow` subclasses, so adding one needs no core/taskservice change. No LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.workflows.discovery import discover_workflows

_CUSTOM_WORKFLOW = '''\
from panopticon.core.state import Complete, State
from panopticon.core.workflow import Workflow


class Custom(Workflow):
    name = "custom"

    class Only(State):
        label = "ONLY"
        transitions = (Complete,)

    initial = Only
'''


def test_discovers_the_builtin_workflows() -> None:
    registry = discover_workflows()
    assert {"spike", "github-peer-reviewed"} <= set(registry)  # built-ins, keyed by name
    assert registry["spike"].name == "spike"  # instances, validated on construction


def test_discovers_a_workflow_dropped_on_the_path(tmp_path: Path) -> None:
    (tmp_path / "custom_wf.py").write_text(_CUSTOM_WORKFLOW)
    registry = discover_workflows(path=str(tmp_path))
    assert "custom" in registry and registry["custom"].name == "custom"  # no core change needed
    assert {"spike", "github-peer-reviewed"} <= set(registry)  # still includes the built-ins


def test_ignores_underscored_and_non_workflow_files(tmp_path: Path) -> None:
    (tmp_path / "_private.py").write_text(_CUSTOM_WORKFLOW.replace('"custom"', '"private"'))
    (tmp_path / "notes.py").write_text("X = 1\n")  # no Workflow subclass
    registry = discover_workflows(path=str(tmp_path))
    assert "private" not in registry  # underscore-prefixed modules are skipped


def test_duplicate_name_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "dupe.py").write_text(_CUSTOM_WORKFLOW.replace('"custom"', '"spike"'))  # clashes with built-in
    with pytest.raises(ValueError, match="duplicate workflow name 'spike'"):
        discover_workflows(path=str(tmp_path))
