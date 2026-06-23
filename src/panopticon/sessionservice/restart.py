"""Restart a repo's running task containers so they pick up freshly-written credentials.

`panopticon login` writes a repo's OAuth creds into its per-repo volume, but the agent wires a
task's auth **once at spawn** (`container/agent.py:main()` links the creds file in and seeds the
account). A container that was already running stays on its old (or absent) auth until it respawns.

This restarts each of the repo's **live** task containers the same way the dashboard's `R` does:
stop the container (`LocalRunner.stop` → kill its tmux session + remove it) and release its claim,
so the host daemon's spawn loop re-claims and respawns it. The fresh container re-runs the
launch-time bootstrap against the now-populated volume and resumes its session via
``claude --continue`` (the per-task config volume persists). It relies on the host daemon running
to respawn — the same assumption as `R`. LLM-free.
"""

from __future__ import annotations

from panopticon.client import TaskServiceClient
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.local_runner import LocalRunner


def restart_repo_containers(
    client: TaskServiceClient, runner: LocalRunner, repo_id: str
) -> list[str]:
    """Restart every live task container of ``repo_id``; return the restarted task ids.

    Targets only non-terminal tasks with a live registration — a down or unspawned task picks up
    the new creds on its natural next spawn, so we don't force-respawn those. We must ``stop`` (not
    just release): ``spawn`` force-removes a stale container by name but doesn't kill its tmux
    session, which would otherwise block the respawn.
    """
    restarted: list[str] = []
    for task in client.list_tasks():
        if task["repo_id"] != repo_id or task["state"] in TERMINAL_LABELS:
            continue
        registrations = client.list_registrations(task["id"])
        if not registrations:
            continue  # no live container to restart
        runner.stop(registrations[0]["container_id"])  # session == container id (runner names it)
        client.release(task["id"])  # back to unclaimed → the host daemon re-claims + respawns
        restarted.append(task["id"])
    return restarted
