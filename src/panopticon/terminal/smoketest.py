"""Undocumented ``panopticon smoketest`` — evaluate whether the CI smoke test reached its goal.

The smoke workflow (``.github/workflows/smoke.yml``) drives ``panopticon quickstart`` and must
assert that the ``setup-repo`` shell task reaches the ``claude setup-token`` mint prompt — the last
step before the interactive OAuth CI can't complete. This encapsulates that check as a single
command so the CI step isn't an inline shell + python heredoc: it waits for the runner to spawn the
setup-repo task, polls its host tmux pane for the mint prompt, and exits ``0`` when it appears (or
``1`` with diagnostics on timeout).

A CI-only helper — the subcommand is registered with ``help=argparse.SUPPRESS`` so it stays out of
``panopticon --help``. Pure and injectable (the tmux runner, sleep and output sink are parameters)
so it's unit-testable without tmux or a task service.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import TMUX_SOCKET, session_name
from panopticon.terminal.setup_repo_task import SETUP_REPO_WORKFLOW

#: The line ``setup_repo.sh`` prints right before it would run ``claude setup-token``. Reaching it
#: means the quickstart flow got all the way to the mint step (without CI completing the interactive
#: OAuth) — the smoke test's success signal.
MINT_MARKER = "mint one with 'claude setup-token'"

#: Run a ``tmux`` subcommand (already scoped to the panopticon socket) and return its stdout.
TmuxRunner = Callable[[Sequence[str]], str]


def _tmux_runner(socket: str) -> TmuxRunner:
    """A :data:`TmuxRunner` that shells out to ``tmux -L <socket> …`` (stdout, empty on error)."""

    def run(args: Sequence[str]) -> str:
        return subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True).stdout

    return run


def _find_setup_repo_task(client: TaskServiceClient) -> str | None:
    """The id of the (single) ``setup-repo`` task the runner spawned, or ``None`` if not yet there."""
    for task in client.list_tasks():
        if task.get("workflow") == SETUP_REPO_WORKFLOW:
            return str(task["id"])
    return None


def evaluate(
    client: TaskServiceClient,
    *,
    timeout: int = 60,
    run_tmux: TmuxRunner,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    """Return ``0`` if the setup-repo task reaches the mint prompt within ``timeout``, else ``1``.

    Waits (up to ``timeout`` seconds) for the runner to spawn the setup-repo task, then polls its
    ``panopticon-<id>`` host tmux pane (a further ``timeout`` seconds) for :data:`MINT_MARKER`. On
    failure it dumps the tmux sessions and the pane's scrollback via ``out`` before returning ``1``.
    """
    task_id: str | None = None
    for _ in range(timeout):
        task_id = _find_setup_repo_task(client)
        if task_id:
            break
        sleep(1)
    if not task_id:
        out(f"smoketest: no setup-repo task appeared within {timeout}s")
        return 1

    session = session_name(task_id)
    out(f"smoketest: waiting for {session} to reach the token-mint prompt...")
    for _ in range(timeout):
        if MINT_MARKER in run_tmux(["capture-pane", "-t", session, "-p"]):
            out("smoketest: reached the token-mint prompt")
            return 0
        sleep(1)

    out(f"smoketest: {session} did not reach the token-mint prompt within {timeout}s")
    out("--- tmux list-sessions ---")
    out(run_tmux(["list-sessions"]))
    out("--- setup-repo pane ---")
    out(run_tmux(["capture-pane", "-t", session, "-p", "-S", "-"]))
    return 1


def run(client: TaskServiceClient, *, timeout: int = 60, socket: str = TMUX_SOCKET) -> int:
    """Wire :func:`evaluate` to the real tmux CLI on ``socket`` (the ``panopticon smoketest`` entry)."""
    return evaluate(client, timeout=timeout, run_tmux=_tmux_runner(socket))
