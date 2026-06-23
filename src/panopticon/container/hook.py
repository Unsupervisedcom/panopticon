"""The turn-flip hook callback (`python -m panopticon.container.hook <user|agent>`).

claude's Stop / UserPromptSubmit hooks invoke this to flip the live turn (the Slice 4 contract).
It reads the task from the container's env and POSTs `set_turn`. claude-specific wiring (M3);
the deterministic turn mechanism it calls lives in the task service. It sets only the turn, so a
deliberate `blocked` marker survives.

**Background-task gate (Stop → ``user``).** A background task (a Bash command launched with
``run_in_background``, or the ``Monitor`` tool) keeps running after the agent's visible turn ends.
When it completes, claude re-invokes the agent with a synthetic mid-turn message — it does **not**
fire ``UserPromptSubmit``, so a turn flipped to ``user`` would never flip back even though the
agent is about to be woken and keep working. So on the Stop event we read the hook's stdin payload
and, if it reports any **live** background task (``background_tasks`` array, claude ≥ v2.1.145), we
**skip** the flip and leave the turn on the agent. The agent flips to ``user`` on the eventual real
stop with nothing in flight. If the payload lacks the field (older claude / empty stdin), we
degrade to the original behaviour and flip to ``user``.

On the **user's turn** (UserPromptSubmit → ``agent``) it also prints, into the agent's context
(claude adds a UserPromptSubmit hook's stdout there), the **current-phase briefing** — which state
the task is in and what that phase expects — so the agent knows where it is in the workflow instead
of charging ahead. While the task is still unslugged it additionally prints the provisioning nudge
(ADR 0011 §3), reminding the agent to run the `provision` skill once it can name the task.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from typing import IO, Any

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.provisioning import PROVISION_NUDGE

#: A background task's ``status`` value counts as *finished* (no longer in flight) only if it's one
#: of these. Anything else — including a missing/unknown status — is treated as live, so we err
#: toward keeping the turn on the agent rather than prematurely handing it back.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "error"})


def _read_payload(stdin: IO[str]) -> dict[str, Any]:
    """Tolerantly parse the hook's stdin JSON; empty/invalid input yields an empty payload."""
    try:
        raw = stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _has_live_background_task(payload: dict[str, Any]) -> bool:
    """Whether the Stop payload reports a still-running background task (Bash bg / Monitor).

    Reads claude's ``background_tasks`` array (claude ≥ v2.1.145; absent on older builds, where this
    is simply ``False`` and the turn flips as before). An entry is live unless its ``status`` is a
    known terminal one (see :data:`_TERMINAL_STATUSES`)."""
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, dict):
            return True  # unrecognised shape → assume live (don't hand the turn back prematurely)
        status = task.get("status")
        if not isinstance(status, str) or status.strip().lower() not in _TERMINAL_STATUSES:
            return True
    return False


def main(
    argv: Sequence[str] | None = None,
    *,
    client: TaskServiceClient | None = None,
    stdin: IO[str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("user", "agent"):
        print("usage: python -m panopticon.container.hook <user|agent>", file=sys.stderr)
        return 2
    env = os.environ
    actor, task_id = args[0], env["PANOPTICON_TASK_ID"]
    client = client or TaskServiceClient(httpx.Client(base_url=env["PANOPTICON_SERVICE_URL"]))
    # Stop (actor == "user"): don't hand the turn back while a background task is still running —
    # its completion re-invokes the agent without a UserPromptSubmit, so the turn would never
    # return to the agent. Leave it on the agent; the next real stop with nothing in flight flips.
    if actor == "user" and _has_live_background_task(_read_payload(stdin or sys.stdin)):
        return 0
    client.set_turn(task_id, actor)
    # UserPromptSubmit (actor == "agent"): ground the agent in its current phase, and (while the
    # task is unslugged) nudge toward provisioning. claude adds this hook's stdout to its context.
    if actor == "agent":
        print(client.get_briefing(task_id))
        if client.get_task(task_id).get("slug") is None:
            print(PROVISION_NUDGE)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
