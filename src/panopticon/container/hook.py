"""The turn-flip hook callback (`python -m panopticon.container.hook <user|agent>`).

claude's Stop / UserPromptSubmit hooks invoke this to flip the live turn (the Slice 4 contract).
It reads the task from the container's env and POSTs `set_turn`. claude-specific wiring (M3);
the deterministic turn mechanism it calls lives in the task service. It sets only the turn, so a
deliberate `blocked` marker survives.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import httpx

from panopticon.client import TaskServiceClient


def main(argv: Sequence[str] | None = None, *, client: TaskServiceClient | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("user", "agent"):
        print("usage: python -m panopticon.container.hook <user|agent>", file=sys.stderr)
        return 2
    env = os.environ
    client = client or TaskServiceClient(httpx.Client(base_url=env["PANOPTICON_SERVICE_URL"]))
    client.set_turn(env["PANOPTICON_TASK_ID"], args[0])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
