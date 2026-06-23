"""The turn-flip hook callback (`python -m panopticon.container.hook <user|agent>`).

claude's Stop / UserPromptSubmit hooks invoke this to flip the live turn (the Slice 4 contract).
It reads the task from the container's env and POSTs `set_turn`. claude-specific wiring (M3);
the deterministic turn mechanism it calls lives in the task service. It sets only the turn, so a
deliberate `blocked` marker survives.

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
from pathlib import Path
from typing import TextIO

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.provisioning import PROVISION_NUDGE

#: Per-message usage keys we sum into the session total — every token the model processed
#: (prompt + completion + both cache tiers).
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def session_tokens(transcript_path: str) -> int:
    """Total the tokens across a claude session transcript (JSONL).

    Each assistant line carries ``message.usage``; we sum the four token tiers in :data:`_USAGE_KEYS`
    across every such line — the honest "tokens the model processed" for the whole session. Pure and
    LLM-free, so it's unit-tested with a fixture transcript. Tolerant of a missing file, blank or
    malformed lines, and absent usage keys (each counted as 0), so a transcript hiccup yields a
    best-effort number rather than raising."""
    total = 0
    try:
        with Path(transcript_path).open() as lines:
            for line in lines:
                total += _line_tokens(line)
    except OSError:  # no transcript yet / unreadable — nothing to count
        return 0
    return total


def _line_tokens(line: str) -> int:
    """The summed usage on one transcript line, or 0 if it isn't an assistant line with usage."""
    line = line.strip()
    if not line:
        return 0
    try:
        usage = json.loads(line).get("message", {}).get("usage") or {}
    except (ValueError, AttributeError):  # not JSON, or message/usage isn't a dict
        return 0
    return sum(usage[key] for key in _USAGE_KEYS if isinstance(usage.get(key), int))


def _report_tokens(client: TaskServiceClient, task_id: str, stdin: TextIO) -> None:
    """Best-effort: read the Stop hook's stdin JSON, total the named transcript, record it.

    Any failure — no/!JSON stdin, no ``transcript_path``, a REST error — is swallowed: token
    accounting must never break the turn-flip the hook exists for."""
    try:
        transcript = json.load(stdin).get("transcript_path")
        if transcript:
            client.set_tokens_used(task_id, session_tokens(transcript))
    except (ValueError, OSError, AttributeError, httpx.HTTPError):
        pass


def main(
    argv: Sequence[str] | None = None,
    *,
    client: TaskServiceClient | None = None,
    stdin: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("user", "agent"):
        print("usage: python -m panopticon.container.hook <user|agent>", file=sys.stderr)
        return 2
    env = os.environ
    actor, task_id = args[0], env["PANOPTICON_TASK_ID"]
    client = client or TaskServiceClient(httpx.Client(base_url=env["PANOPTICON_SERVICE_URL"]))
    client.set_turn(task_id, actor)
    # UserPromptSubmit (actor == "agent"): ground the agent in its current phase, and (while the
    # task is unslugged) nudge toward provisioning. claude adds this hook's stdout to its context.
    if actor == "agent":
        print(client.get_briefing(task_id))
        if client.get_task(task_id).get("slug") is None:
            print(PROVISION_NUDGE)
    # Stop (actor == "user"): the agent's turn just ended — record the session's cumulative token
    # usage from the transcript the hook payload points at. Best-effort, and silent (no stdout).
    else:
        _report_tokens(client, task_id, stdin or sys.stdin)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
