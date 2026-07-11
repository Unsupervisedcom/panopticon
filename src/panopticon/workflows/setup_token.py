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

import textwrap
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


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

        The session service injects ``PANOPTICON_SERVICE_URL``/``PANOPTICON_TASK_ID``, so the
        script advances itself over REST. It pauses on the minted token afterwards so the operator
        (attached to the session) can copy it into the repo's env-file before closing."""
        return textwrap.dedent(
            """\
            echo "Running 'claude setup-token' — follow the prompts to mint a token."
            echo
            if claude setup-token; then
                echo
                echo "Token minted — marking this task complete."
                curl --silent --show-error --fail --request POST \\
                    "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/operations/advance" \\
                    >/dev/null \\
                    || echo "warning: could not mark the task complete via $PANOPTICON_SERVICE_URL"
                echo
                echo "Copy the token shown above into the repo's env-file as CLAUDE_CODE_OAUTH_TOKEN."
                printf 'Press Enter to close this session. '
                read _
            else
                echo "claude setup-token failed or was cancelled — leaving the task unchanged."
            fi
            """
        )
