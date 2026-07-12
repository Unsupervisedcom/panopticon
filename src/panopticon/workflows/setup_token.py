"""The SetupToken workflow — a **shell** workflow (no container) that mints a Claude auth token.

The first example of ``runner_type = "shell"`` (ADR 0012 retired ``panopticon login``; container
auth is now just a non-rotating ``claude setup-token`` the operator adds to a repo's env-file).
Rather than spawn a task container + agent, the session service runs :meth:`shell_script` directly
in a host tmux session: the operator attaches (``t`` in the dashboard), completes the interactive
``claude setup-token`` OAuth flow, and the script marks the task ``COMPLETE`` over REST. No image,
no per-task clone, no LLM — just a host shell doing one operator chore.

``RUNNING → {COMPLETE, DROPPED}``. The single state carries no responsibilities (a shell task has
no agent to gate); the script itself drives the advance to COMPLETE on success.
"""

from __future__ import annotations

import importlib.resources
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow

#: The shell script the workflow runs, kept in a sibling ``setup_token.sh`` so it's edited (and
#: shell-linted) as a real script rather than a Python string. Read once at import.
_SCRIPT = (importlib.resources.files("panopticon.workflows") / "setup_token.sh").read_text()


class SetupToken(Workflow):
    """A no-container utility workflow: run ``claude setup-token`` on the host to mint a token.

    ``runner_type = "shell"`` routes it to the session service's shell runner instead of the
    Docker one. ``opt_in`` keeps this operator utility out of the picker unless a repo enables it.
    """

    name: ClassVar[str] = "setup-token"
    runner_type: ClassVar[str] = "shell"
    opt_in: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Mint a Claude auth token on the host (runs `claude setup-token` in a shell, no "
        "container) — attach to complete the OAuth flow, then copy the token into the repo env-file."
    )

    class Running(InitialState):
        label = "RUNNING"
        description = "Run `claude setup-token` in a host shell; the script marks the task complete on success."
        transitions = (Complete,)  # advance → COMPLETE; + DROPPED inherited

    initial = Running

    def shell_script(self) -> str:
        """Run ``claude setup-token`` interactively; on success, advance the task to COMPLETE.

        The script lives in the sibling ``setup_token.sh``. The session service injects
        ``PANOPTICON_SERVICE_URL``/``PANOPTICON_TASK_ID``, so the script advances itself over REST;
        it pauses on the minted token afterwards so the operator (attached to the session) can copy
        it into the repo's env-file before closing."""
        return _SCRIPT
