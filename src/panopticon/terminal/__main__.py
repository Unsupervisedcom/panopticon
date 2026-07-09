"""``panopticon`` / ``python -m panopticon.terminal`` — the operator CLI.

`panopticon` (or `panopticon console`) runs the session supervisor (ADR 0009): the dashboard,
plus handing the terminal to a task's tmux on `t` and rejoining on detach. `panopticon dashboard`
runs the dashboard once without the attach loop; `panopticon tasks` lists tasks as plain text.
`panopticon start-runner <host>` SSHes to a remote host, opens a reverse port forward so the
remote runner can reach the local task service, and starts the session service there.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.terminal.launch import (
    DEFAULT_CACHE_ROOT,
    DEFAULT_IMAGE,
    DEFAULT_PYTHON,
    DEFAULT_TASKS_ROOT,
    start_runner,
)

DEFAULT_SERVICE_URL = "http://localhost:8000"


def main(
    argv: Sequence[str] | None = None,
    *,
    client: TaskServiceClient | None = None,
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

    sr = sub.add_parser(
        "start-runner",
        help="start a session-service runner locally (--local) or on a remote host via SSH",
    )
    sr.add_argument(
        "host",
        nargs="?",
        default=None,
        help="remote host to SSH to (omit when using --local)",
    )
    sr.add_argument(
        "--local",
        action="store_true",
        help="run the session service on this machine instead of SSH-ing to a remote host; "
             "registers with no hostname so locally-claimed tasks attach without SSH",
    )
    sr.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="task service URL (default: $PANOPTICON_SERVICE_URL or http://localhost:8000)",
    )
    sr.add_argument(
        "--remote-port",
        type=int,
        default=None,
        metavar="PORT",
        help="port forwarded on the remote host (default: same as local service port)",
    )
    sr.add_argument(
        "--runner-id",
        default=None,
        metavar="ID",
        help="runner id to register as (default: <host>, or 'local' with --local)",
    )
    sr.add_argument(
        "--container-service-url",
        default=None,
        metavar="URL",
        help="URL injected into containers to reach the task service (default: derived from tunnel port)",
    )
    sr.add_argument(
        "--no-tunnel",
        dest="tunnel",
        action="store_false",
        help="skip the reverse port forward; use when the task service has a routable address",
    )
    sr.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"task container image (default: {DEFAULT_IMAGE})",
    )
    sr.add_argument(
        "--tasks-root",
        default=DEFAULT_TASKS_ROOT,
        metavar="PATH",
        help=f"tasks root directory (default: {DEFAULT_TASKS_ROOT})",
    )
    sr.add_argument(
        "--cache-root",
        default=DEFAULT_CACHE_ROOT,
        metavar="PATH",
        help=f"cache root directory (default: {DEFAULT_CACHE_ROOT})",
    )
    sr.add_argument(
        "--python",
        default=None,
        metavar="CMD",
        help=f"Python interpreter; multi-word values are split (e.g. 'uv run python') "
             f"(default: sys.executable with --local, {DEFAULT_PYTHON} for remote)",
    )

    args = parser.parse_args(argv)

    if args.command == "start-runner":
        if args.local and args.host:
            sr.error("--local and host are mutually exclusive")
        if not args.local and not args.host:
            sr.error("host is required unless --local is specified")
        start_runner(
            args.host,
            local=args.local,
            local_service_url=args.service_url,
            remote_port=args.remote_port,
            runner_id=args.runner_id,
            container_service_url=args.container_service_url,
            tunnel=args.tunnel,
            image=args.image,
            tasks_root=args.tasks_root,
            cache_root=args.cache_root,
            python=args.python,
        )
        return 0

    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    if args.command == "tasks":
        for t in client.list_tasks():
            print(f"{t['id']}  {t['state']:<10}  {t['turn']:<5}  {t['slug'] or '-'}")
    elif args.command == "dashboard":
        from panopticon.terminal.console import make_runner_switch, make_service_switch, switch_to
        from panopticon.terminal.dashboard import run

        on_switch = None
        on_service = None
        on_runner = None
        if args.switch_file:  # run under the supervisor: report `t`/`s`/`u` picks via the switch-file
            switch_file = Path(args.switch_file)
            on_switch = lambda session, host=None: switch_to(session, host=host, switch_file=switch_file)  # noqa: E731
            on_service = make_service_switch(switch_file)
            on_runner = make_runner_switch(switch_file)
        # Same env/default as the task service (shared DEFAULT_ARTIFACTS): when the dashboard shares
        # the store's filesystem, `a`'s `e` opens the on-disk artifact in place.
        from panopticon.taskservice.artifacts_fs import DEFAULT_ARTIFACTS

        artifacts_root = os.environ.get("PANOPTICON_ARTIFACTS", DEFAULT_ARTIFACTS)
        run(
            client, on_switch=on_switch, on_service=on_service, on_runner=on_runner,
            artifacts_root=artifacts_root,
        )
    else:  # default / "console"
        from panopticon.terminal.console import run_console_local

        run_console_local(args.service_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
