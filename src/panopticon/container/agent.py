"""The in-container **agent launcher** — what the runner's tmux pane runs.

It prepares the agent CLI's surface from the active workflow, then `exec`s the agent. This is
the only LLM-bearing path (the determinism invariant): the **bootstrap** (render the workflow's
skills to the CLI, point it at the repo's creds) is deterministic and unit-tested with fakes;
the **launch** (real `claude`) is injectable and only runs for real in a `skipif`-gated
integration / a live container — never in CI.

The container's entrypoint (`python -m panopticon.container`) stays the liveness/heartbeat loop;
this runs alongside it in the tmux pane, so `tmux attach` reaches the live agent.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.container.skills import write_commands
from panopticon.core.models import Skill

#: The agent CLI's config/creds dir inside the container — the repo's OAuth creds volume mount
#: (matches the runner's CREDS_MOUNT). claude reads/writes its credentials here.
CREDS_DIR = "/creds"


def render_skills(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's skills to the agent CLI surface (`.claude/commands/`)."""
    skills = [Skill(**s) for s in client.list_skills(task_id)]
    return write_commands(skills, home)


def _claude_argv(config_dir: Path, cwd: Path) -> list[str]:
    """`claude` argv, resuming the project's most recent conversation if one exists.

    claude keeps per-project transcripts under ``<config>/projects/<cwd with '/' → '-'>``; when
    one is there (e.g. the pane or container restarted, or the operator re-attached) we
    ``--continue`` it instead of starting a fresh conversation. Sessions persist because the
    config dir is the repo's creds volume. If our path encoding ever misses claude's, we simply
    start fresh — a safe degradation, never a broken launch.
    """
    argv = ["claude"]
    project = config_dir / "projects" / str(cwd).replace("/", "-")
    if any(project.glob("*.jsonl")):
        argv.append("--continue")
    return argv


def _exec_claude() -> None:  # pragma: no cover - real LLM; skipif-gated / live only
    """Replace this process with `claude` (resuming the session if any), pointed at the creds."""
    argv = _claude_argv(Path(CREDS_DIR), Path.cwd())
    os.execvpe(argv[0], argv, {**os.environ, "CLAUDE_CONFIG_DIR": CREDS_DIR})


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    home: Path | None = None,
    launch: Callable[[], None] = _exec_claude,
) -> None:
    """Bootstrap the agent CLI from the active workflow, then launch the agent."""
    env = os.environ
    client = client_factory(env["PANOPTICON_SERVICE_URL"])
    render_skills(client, env["PANOPTICON_TASK_ID"], home or Path.home())
    launch()


if __name__ == "__main__":  # pragma: no cover
    main()
