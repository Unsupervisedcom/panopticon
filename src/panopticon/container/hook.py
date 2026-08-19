"""The turn-flip hook callback (`python -m panopticon.container.hook <user|agent> [prompt|stop]`).

claude's Stop / UserPromptSubmit hooks invoke this to flip the live turn (the Slice 4 contract);
so do the PreToolUse / PostToolUse hooks matched to ``AskUserQuestion``, so the turn reads *user*
while the agent is asking the user something and *agent* once it's answered. It reads the task from
the container's env and POSTs `set_turn`. claude-specific wiring (M3); the deterministic turn
mechanism it calls lives in the task service. It sets only the turn, so a deliberate `blocked`
marker survives.

The first argument is the turn to set; the optional second selects an **event side-effect** — kept
distinct from the actor so the bare question hooks (`hook user` / `hook agent`) are a pure turn flip:

- ``prompt`` (UserPromptSubmit → ``agent``): print, into the agent's context (claude adds a
  UserPromptSubmit hook's stdout there), the **current-phase briefing** — which state the task is in
  and what that phase expects — so the agent knows where it is instead of charging ahead. While the
  task is still unslugged it additionally prints the provisioning nudge (ADR 0011 §3), reminding the
  agent to run the `provision` skill once it can name the task.
- ``stop`` (Stop → ``user``): gate the turn flip on background work. A
  background task (a Bash command launched with ``run_in_background``, the ``Monitor`` tool, or a
  background **agent**) keeps running after the agent's visible turn ends; its completion re-invokes
  the agent with a synthetic message — *not* a ``UserPromptSubmit`` — so a turn flipped to ``user``
  would never flip back even though the agent is about to be woken and keep working. So if the Stop
  payload reports any **live** background task (``background_tasks`` array, claude ≥ v2.1.145) we
  leave the turn on the agent; it flips to ``user`` on the eventual real stop with nothing in flight.
  If the payload lacks the field (older claude / empty stdin) we degrade to the plain flip.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from typing import Any, TextIO

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.provisioning import PROVISION_NUDGE

#: A background task's ``status`` value counts as *finished* (no longer in flight) only if it's one
#: of these. Anything else — including a missing/unknown status — is treated as live, so we err
#: toward keeping the turn on the agent rather than prematurely handing it back.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "error"})


def _read_payload(stdin: TextIO) -> dict[str, Any]:
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
    """Whether the Stop payload reports a still-running background task.

    Reads claude's ``background_tasks`` array (claude ≥ v2.1.145; absent on older builds, where this
    is simply ``False`` and the turn flips as before). An entry is live unless its ``status`` is a
    known terminal one (see :data:`_TERMINAL_STATUSES`).

    Deliberately **type-agnostic**: it ignores each entry's ``type`` so it covers every kind of
    background work that re-wakes the agent — ``shell`` (Bash ``run_in_background``), ``monitor``,
    and background **agents** (``subagent``/``workflow``/``teammate``/``cloud_session``/``mcp_task``)
    alike. They all re-invoke the agent on completion without a UserPromptSubmit, so the turn must
    stay on the agent for all of them."""
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
    stdin: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if (
        not 1 <= len(args) <= 2
        or args[0] not in ("user", "agent")
        or args[1:] not in ([], ["prompt"], ["stop"])
    ):
        print(
            "usage: python -m panopticon.container.hook <user|agent> [prompt|stop]", file=sys.stderr
        )
        return 2
    env = os.environ
    actor, event = args[0], (args[1] if len(args) == 2 else None)
    task_id = env["PANOPTICON_TASK_ID"]
    client = client or TaskServiceClient(httpx.Client(base_url=env["PANOPTICON_SERVICE_URL"]))
    # `stop` (Stop): the agent's turn just ended. Read the payload for the background_tasks list and
    # decide the turn: don't hand it back while a background task is still running, since the task's
    # completion re-invokes the agent without a UserPromptSubmit and a flip to `user` would never
    # flip back. Leave it on the agent; the next real stop with nothing in flight flips. (This gate
    # is the Stop event only, not the bare AskUserQuestion `hook user` flip — there the agent is
    # genuinely awaiting the user.)
    if event == "stop":
        payload = _read_payload(stdin or sys.stdin)
        if _has_live_background_task(payload):
            return 0
    client.set_turn(task_id, actor)
    # `prompt` (UserPromptSubmit): ground the agent in its current phase, and (while the task is
    # unslugged) nudge toward provisioning. claude adds this hook's stdout to its context.
    if event == "prompt":
        print(client.get_briefing(task_id))
        if client.get_task(task_id).get("slug") is None:
            print(PROVISION_NUDGE)
    # No event (the AskUserQuestion PreToolUse/PostToolUse hooks): a pure turn flip, no side-effects.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
