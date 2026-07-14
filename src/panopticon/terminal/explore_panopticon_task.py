"""Shared helper for launching an ``explore-panopticon`` task (a guided tour of the codebase).

The ``explore-panopticon`` workflow is hidden from the pickers, so it's created directly — by the
dashboard's repos-modal explore hotkey. Going through here keeps the workflow name and the task's
memo in a single source of truth (mirrors ``setup_repo_task``).
"""

from __future__ import annotations

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.workflows.explore_panopticon import ExplorePanopticon

#: The workflow launched to open a throwaway panopticon clone + ``claude`` to explore it.
EXPLORE_PANOPTICON_WORKFLOW = ExplorePanopticon.name

#: The memo seeded on an explore-panopticon task — the task isn't repo-specific (it always clones
#: panopticon), so the memo names the tour rather than the repo.
EXPLORE_PANOPTICON_MEMO = "Explore and understand panopticon."


def create_explore_panopticon_task(client: TaskServiceClient, repo_id: str) -> JsonObj:
    """Create an ``explore-panopticon`` task for ``repo_id`` seeded with the standard memo.

    ``repo_id`` only supplies the task's repo (and the host shell's ``claude`` credentials, via the
    repo's env-file); the clone is always panopticon regardless of which repo is chosen."""
    return client.create_task(repo_id, EXPLORE_PANOPTICON_WORKFLOW, EXPLORE_PANOPTICON_MEMO)
