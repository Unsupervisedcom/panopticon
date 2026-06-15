"""``panopticon`` / ``python -m panopticon.terminal`` — the operator CLI.

`panopticon` (or `panopticon dashboard`) launches the Textual dashboard; `panopticon tasks`
lists tasks as plain text over REST.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import httpx

from panopticon.client import TaskServiceClient

DEFAULT_SERVICE_URL = "http://localhost:8000"


def main(argv: Sequence[str] | None = None, *, client: TaskServiceClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="panopticon", description="panopticon operator CLI")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="task service base URL",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("dashboard", help="launch the dashboard (default)")
    sub.add_parser("tasks", help="list tasks as plain text")
    args = parser.parse_args(argv)

    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    if args.command == "tasks":
        for t in client.list_tasks():
            print(f"{t['id']}  {t['state']:<10}  {t['turn']:<5}  {t['slug'] or '-'}")
    else:  # default / "dashboard"
        from panopticon.terminal.dashboard import run

        run(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
