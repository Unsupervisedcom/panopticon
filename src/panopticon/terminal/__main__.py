"""``panopticon`` / ``python -m panopticon.terminal`` — the operator CLI.

`panopticon` (or `panopticon console`) runs the session supervisor (ADR 0009): the dashboard,
plus handing the terminal to a task's tmux on `t` and rejoining on detach. `panopticon dashboard`
runs the dashboard once without the attach loop; `panopticon tasks` lists tasks as plain text;
`panopticon login <repo> [cmd...]` populates a repo's creds volume interactively (ADR 0007).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from panopticon.client import TaskServiceClient

if TYPE_CHECKING:  # avoid importing sessionservice at module load
    from panopticon.sessionservice.local_runner import LocalRunner

DEFAULT_SERVICE_URL = "http://localhost:8000"


def main(
    argv: Sequence[str] | None = None,
    *,
    client: TaskServiceClient | None = None,
    runner: LocalRunner | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="panopticon", description="panopticon operator CLI")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="task service base URL",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("console", help="session supervisor: dashboard + attach loop (default)")
    dash = sub.add_parser("dashboard", help="run the dashboard once, without the attach loop")
    # Set by the supervisor (ADR 0009): the dashboard runs inside tmux, so it reports the session
    # the operator picked with `t` by writing it here instead of returning it in-process.
    dash.add_argument("--switch-file", help=argparse.SUPPRESS)
    sub.add_parser("tasks", help="list tasks as plain text")
    login_p = sub.add_parser("login", help="populate a repo's creds volume interactively")
    login_p.add_argument("repo")
    login_p.add_argument("cmd", nargs="*", help="login command to run (default: claude)")
    args = parser.parse_args(argv)

    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    if args.command == "tasks":
        for t in client.list_tasks():
            print(f"{t['id']}  {t['state']:<10}  {t['turn']:<5}  {t['slug'] or '-'}")
    elif args.command == "login":
        creds = client.get_repo(args.repo).get("creds_volume")
        if not creds:
            print(f"repo {args.repo!r} has no creds_volume configured", file=sys.stderr)
            return 1
        from panopticon.sessionservice.local_runner import LocalRunner

        (runner or LocalRunner(args.service_url)).login(creds, args.cmd or ["claude"])
    elif args.command == "dashboard":
        from panopticon.terminal.console import make_service_switch, switch_to
        from panopticon.terminal.dashboard import run

        on_switch = None
        on_service = None
        if args.switch_file:  # run under the supervisor: report `t`/`s` picks via the switch-file
            switch_file = Path(args.switch_file)
            on_switch = lambda session: switch_to(session, switch_file=switch_file)  # noqa: E731
            on_service = make_service_switch(switch_file)
        # Same env/default as the task service (shared DEFAULT_ARTIFACTS): when the dashboard shares
        # the store's filesystem, `a`'s `e` opens the on-disk artifact in place.
        from panopticon.taskservice.artifacts_fs import DEFAULT_ARTIFACTS

        artifacts_root = os.environ.get("PANOPTICON_ARTIFACTS", DEFAULT_ARTIFACTS)
        # The repos screen's `l` hook: log in to a repo's creds volume interactively (default
        # command `claude`), the same flow as `panopticon login`. Import lazily so the dashboard
        # path doesn't pull in sessionservice at module load.
        from panopticon.sessionservice.local_runner import LocalRunner

        login = lambda creds: LocalRunner(args.service_url).login(creds, ["claude"])  # noqa: E731
        run(
            client, on_switch=on_switch, on_service=on_service, login=login,
            artifacts_root=artifacts_root,
        )
    else:  # default / "console"
        from panopticon.terminal.console import run_console_local

        run_console_local(args.service_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
