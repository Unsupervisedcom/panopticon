"""Shared helper for launching a ``setup-repo`` task (host-side repo setup).

The ``setup-repo`` workflow is hidden from the pickers, so it's created directly — by the
dashboard's ``s`` hotkey and by ``panopticon quickstart``. Both go through here so the workflow
name and the task's memo have a single source of truth.
"""

from __future__ import annotations

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.workflows.setup_repo import SetupRepo

#: The workflow launched to run host-side repo setup (``claude setup-token`` in a host shell).
SETUP_REPO_WORKFLOW = SetupRepo.name


def setup_repo_memo(name: str) -> str:
    """The memo seeded on a setup-repo task, naming the repo it's for."""
    return f"Set up the {name} repo."


def create_setup_repo_task(client: TaskServiceClient, repo_id: str, name: str) -> JsonObj:
    """Create a ``setup-repo`` task for ``repo_id`` seeded with the standard memo."""
    return client.create_task(repo_id, SETUP_REPO_WORKFLOW, setup_repo_memo(name))
