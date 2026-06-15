"""``panopticon`` / ``python -m panopticon.terminal`` — the operator CLI.

For now: ``panopticon tasks`` lists tasks (a plain-text read over REST). The Textual dashboard
becomes the default command in a later PR of this slice.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import httpx

from panopticon.terminal.client import DashboardClient

DEFAULT_SERVICE_URL = "http://localhost:8000"


def main(argv: Sequence[str] | None = None, *, client: DashboardClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="panopticon", description="panopticon operator CLI")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="task service base URL",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tasks", help="list tasks")
    args = parser.parse_args(argv)

    client = client or DashboardClient(httpx.Client(base_url=args.service_url))
    if args.command == "tasks":
        for t in client.list_tasks():
            print(f"{t['id']}  {t['state']:<10}  {t['turn']:<5}  {t['slug'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
