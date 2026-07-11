"""``panopticon`` / ``python -m panopticon.terminal`` — the operator CLI.

`panopticon` with no argument (or `panopticon start`) starts everything: runs DB migrations,
starts the task service and session-service runner in background tmux sessions, then opens the
session supervisor (ADR 0009) — the dashboard, plus handing the terminal to a task's tmux on `t`
and rejoining on detach. `panopticon console` opens the supervisor only (assumes services are
already running). `panopticon dashboard` runs the dashboard once without the attach loop;
`panopticon tasks` lists tasks as plain text; `panopticon migrate` applies DB migrations to head
via the bundled Alembic config. `panopticon quickstart` registers panopticon itself as a repo
(idempotent) then starts everything.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient

DEFAULT_SERVICE_URL = "http://localhost:8000"

_PANOPTICON_REPO_ID = "panopticon"
_FALLBACK_GIT_URL = "https://github.com/Unsupervisedcom/panopticon.git"
_SECRETS_TEMPLATE = """\
# Panopticon agent secrets
# Fill in before creating tasks. This file is injected into task containers as environment variables.
#
# Claude authentication — one of:
CLAUDE_CODE_OAUTH_TOKEN=
# ANTHROPIC_API_KEY=
#
# GitHub access token (for creating/reading PRs, issues, etc.)
GH_TOKEN=
"""


def _detect_git_url() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _FALLBACK_GIT_URL


def _ensure_secrets_file() -> str:
    from panopticon.core.dirs import user_config_dir

    secrets_path = user_config_dir() / "panopticon.env"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    if secrets_path.exists():
        print(f"Secrets file already exists: {secrets_path}")
    else:
        secrets_path.write_text(_SECRETS_TEMPLATE)
        print(f"Created secrets template: {secrets_path}")
        print("  → Edit it to add your CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN before creating tasks.")
    return str(secrets_path)


def _wait_for_service(service_url: str, *, timeout: int = 30) -> None:
    import time

    import httpx as _httpx

    deadline = time.monotonic() + timeout
    while True:
        try:
            _httpx.get(f"{service_url}/tasks", timeout=1.0).raise_for_status()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Task service at {service_url} did not respond within {timeout}s"
                )
            time.sleep(1.0)


def _setup_panopticon_repo(client: TaskServiceClient, git_url: str, env_file: str) -> None:
    try:
        client.get_repo(_PANOPTICON_REPO_ID)
        print("Panopticon repo already configured — skipping registration.")
        return
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    client.create_repo(_PANOPTICON_REPO_ID, "panopticon", git_url, env_file=env_file)
    print(f"Registered panopticon repo (git_url={git_url!r}).")
    print(f"  → Secrets file: {env_file}")


def _run_migrate() -> None:
    import importlib.resources

    import alembic.config

    ini_ref = importlib.resources.files("panopticon") / "alembic.ini"
    with importlib.resources.as_file(ini_ref) as ini_path:
        alembic.config.main(argv=["--config", str(ini_path), "upgrade", "head"])


def _start_sessions() -> None:
    import subprocess
    import sys

    python = sys.executable
    for name, cmd in [
        ("service", f"{python} -m panopticon.taskservice 2>&1 | tee /tmp/panopticon-service.log"),
        ("runner", f"{python} -m panopticon.sessionservice.host 2>&1 | tee /tmp/panopticon-runner.log"),
    ]:
        subprocess.run(
            ["tmux", "-L", "panopticon", "kill-session", "-t", name],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "-L", "panopticon", "new-session", "-d", "-s", name, cmd],
            check=True,
        )


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
    sub.add_parser("console", help="session supervisor: dashboard + attach loop (assumes services are running)")
    dash = sub.add_parser("dashboard", help="run the dashboard once, without the attach loop")
    # Set by the supervisor (ADR 0009): the dashboard runs inside tmux, so it reports the session
    # the operator picked with `t` by writing it here instead of returning it in-process.
    dash.add_argument("--switch-file", help=argparse.SUPPRESS)
    sub.add_parser("tasks", help="list tasks as plain text")
    mig = sub.add_parser("migrate", help="apply DB migrations to head (or pass alembic args)")
    mig.add_argument("alembic_args", nargs="*", default=["upgrade", "head"])
    sub.add_parser("build", help="build the base task-container image (panopticon-base)")
    sub.add_parser("host", help="start task service + runner in background tmux sessions (no console)")
    sub.add_parser("start", help="start everything and open the dashboard supervisor")
    sub.add_parser("stop", help="stop task containers and the panopticon tmux server")
    sub.add_parser(
        "quickstart",
        help=(
            "first-time setup: register panopticon as a repo (idempotent), "
            "then start everything and open the dashboard supervisor"
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "migrate":
        import importlib.resources

        import alembic.config

        ini_ref = importlib.resources.files("panopticon") / "alembic.ini"
        with importlib.resources.as_file(ini_ref) as ini_path:
            alembic.config.main(argv=["--config", str(ini_path)] + list(args.alembic_args))
        return 0
    elif args.command == "build":
        from panopticon.sessionservice.images import ImageBuilder

        ImageBuilder().build_base(verbose=True)
        return 0
    elif args.command == "host":
        _run_migrate()
        _start_sessions()
        return 0
    elif args.command == "quickstart":
        _run_migrate()
        _start_sessions()
        _wait_for_service(args.service_url)
        env_file = _ensure_secrets_file()
        git_url = _detect_git_url()
        _setup_panopticon_repo(
            TaskServiceClient(httpx.Client(base_url=args.service_url)), git_url, env_file
        )
        from panopticon.terminal.console import run_console_local

        run_console_local(args.service_url)
        return 0
    elif args.command == "stop":
        import subprocess

        try:
            result = subprocess.run(
                ["docker", "ps", "--all", "--quiet", "--filter", "label=panopticon.task"],
                capture_output=True,
                text=True,
            )
            ids = result.stdout.split() if result.stdout.strip() else []
            if ids:
                subprocess.run(["docker", "rm", "--force"] + ids, check=True)
        except FileNotFoundError:
            pass
        try:
            subprocess.run(
                ["tmux", "-L", "panopticon", "kill-server"],
                capture_output=True,
            )
        except FileNotFoundError:
            pass
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
        # Same default as the task service (shared ARTIFACTS_DIR): when the dashboard shares
        # the store's filesystem, `a`'s `e` opens the on-disk artifact in place.
        from panopticon.core.dirs import ARTIFACTS_DIR

        artifacts_root = ARTIFACTS_DIR
        run(
            client, on_switch=on_switch, on_service=on_service, on_runner=on_runner,
            artifacts_root=artifacts_root,
        )
    else:  # "start", "console", or no subcommand (no subcommand → alias for "start")
        if args.command in (None, "start"):  # "console" assumes services are already running
            _run_migrate()
            _start_sessions()
        from panopticon.terminal.console import run_console_local

        run_console_local(args.service_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
