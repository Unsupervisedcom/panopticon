"""Discover ``Workflow`` subclasses on a package/path — the registry the task service runs on (Slice 8).

Scans the built-in :mod:`panopticon.workflows` package, then ``~/.panopticon/workflows/`` (if it
exists), then an optional extra directory for concrete ``Workflow`` subclasses, instantiates each
(instantiation **validates** it — states, transitions, the required terminal state), and keys them
by ``name``. Adding a user workflow is then just dropping a module in ``~/.panopticon/workflows/``:
no change to ``core`` or ``taskservice``. Duplicate names are rejected so a stray copy can't
silently shadow a built-in. LLM-free.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

from panopticon.core.workflow import Workflow

#: Module namespace for directory-discovered workflows (kept distinct from the package's).
_EXT_PREFIX = "panopticon_workflows_ext"


def _concrete_workflows(module: ModuleType) -> list[type[Workflow]]:
    """The concrete ``Workflow`` subclasses *defined in* ``module`` (not imported into it)."""
    return [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Workflow)
        and obj.__module__ == module.__name__  # defined here, not a re-export of the base/others
        and isinstance(getattr(obj, "name", None), str)
    ]


def _package_modules(package: ModuleType) -> Iterator[ModuleType]:
    for info in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        yield importlib.import_module(info.name)


def _directory_modules(path: Path) -> Iterator[ModuleType]:
    for file in sorted(path.glob("*.py")):
        if file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"{_EXT_PREFIX}.{file.stem}", file)
        if spec is None or spec.loader is None:  # not importable — skip
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


def discover_workflows(
    *,
    package: str = "panopticon.workflows",
    path: str | None = None,
    _home_workflows: Path | None = None,
) -> dict[str, Workflow]:
    """Build the ``{name: workflow}`` registry: built-in ``package`` → ``~/.panopticon/workflows/`` → ``path``.

    Each discovered class is instantiated (validating it); a duplicate ``name`` raises ``ValueError``.
    ``_home_workflows`` overrides the default ``~/.panopticon/workflows`` scan target (tests only).
    """
    modules = list(_package_modules(importlib.import_module(package)))
    home_wf = _home_workflows if _home_workflows is not None else (Path.home() / ".panopticon" / "workflows")
    if home_wf.is_dir():
        modules += list(_directory_modules(home_wf))
    if path:
        modules += list(_directory_modules(Path(path)))
    registry: dict[str, Workflow] = {}
    for module in modules:
        for cls in _concrete_workflows(module):
            workflow = cls()  # instantiation validates the workflow (raises InvalidWorkflow)
            if workflow.name in registry:
                raise ValueError(
                    f"duplicate workflow name {workflow.name!r} (from {cls.__module__}.{cls.__name__})"
                )
            registry[workflow.name] = workflow
    return registry
