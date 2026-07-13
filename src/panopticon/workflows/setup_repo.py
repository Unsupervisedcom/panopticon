"""The SetupRepo workflow — a **shell** workflow (no container) that mints a Claude auth token.

The first example of ``runner_type = "shell"`` (ADR 0012 retired ``panopticon login``; container
auth is now just a non-rotating ``claude setup-token`` the operator adds to a repo's env-file).
Rather than spawn a task container + agent, the session service runs :meth:`shell_script` directly
in a host tmux session: the operator attaches (``t`` in the dashboard), the script checks whether a
credential is already configured and guides them (collect one, or drop the task to add their own),
and on a final Enter it marks the task ``COMPLETE`` over REST and returns them to the dashboard. No
image, no per-task clone, no LLM — just a host shell doing one operator chore.

``RUNNING → {COMPLETE, DROPPED}``. The single state carries no responsibilities (a shell task has
no agent to gate); the script itself drives the advance to COMPLETE when the operator finishes (or
they drop the task instead to keep an existing credential).
"""

from __future__ import annotations

import importlib.resources
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow

#: The shell script the workflow runs, kept in a sibling ``setup_repo.sh`` so it's edited (and
#: shell-linted) as a real script rather than a Python string. Read once at import.
_SCRIPT = (importlib.resources.files("panopticon.workflows") / "setup_repo.sh").read_text()


class SetupRepo(Workflow):
    """A no-container utility workflow: run ``claude setup-token`` on the host to mint a token.

    ``runner_type = "shell"`` routes it to the session service's shell runner instead of the
    Docker one. ``opt_in`` keeps this operator utility out of the picker unless a repo enables it.
    """

    name: ClassVar[str] = "setup-repo"
    runner_type: ClassVar[str] = "shell"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Mint a Claude auth token on the host (runs `claude setup-token` in a shell, no "
        "container) — attach to complete the OAuth flow, then copy the token into the repo env-file."
    )

    class Running(InitialState):
        label = "RUNNING"
        description = "Run `claude setup-token` in a host shell; the script completes the task when the operator finishes."
        transitions = (Complete,)  # advance → COMPLETE; + DROPPED inherited

    initial = Running

    def shell_script(self) -> str:
        """Guide the operator through ``claude setup-token``, then complete the task on a final Enter.

        The script lives in the sibling ``setup_repo.sh``. The session service injects
        ``PANOPTICON_SERVICE_URL``/``PANOPTICON_TASK_ID`` (and sources the repo's secrets), so the
        script checks for an existing credential, optionally collects a new one, and — whatever route
        the operator takes — ends with a summary and a prompt to press Enter, which advances the task
        to COMPLETE over REST and returns them to the dashboard."""
        return _SCRIPT
