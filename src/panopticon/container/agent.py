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

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.container.hooks import write_settings
from panopticon.container.skills import write_commands
from panopticon.core.models import Skill

#: The agent CLI's config/creds dir inside the container — the repo's OAuth creds volume mount
#: (matches the runner's CREDS_MOUNT). claude reads/writes its credentials here.
CREDS_DIR = "/creds"


def render_skills(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's skills to the agent CLI surface (`.claude/commands/`)."""
    skills = [Skill(**s) for s in client.list_skills(task_id)]
    return write_commands(skills, home)


def build_prefill(task: JsonObj) -> str:
    """The initial prompt the agent's input is pre-populated with on launch (PARITY §7)."""
    who = f"task {task['id']}" + (f" ({task['slug']})" if task.get("slug") else "")
    return (
        f"You are the agent for panopticon {who}. Workflow: {task['workflow']}; "
        f"current state: {task['state']}.\n"
        "Do the work for this stage, resolve your responsibilities, then use the core commands "
        "(/advance, /drop) and this workflow's skills to move the task forward."
    )


def _claude_argv(config_dir: Path, cwd: Path, prefill: str) -> list[str]:
    """`claude` argv: resume the project's most recent conversation if one exists (it already has
    context); otherwise start fresh, prefilled with the orientation prompt.

    claude keeps per-project transcripts under ``<config>/projects/<cwd with '/' → '-'>`` (which
    persists across pane/container restarts, since the config dir is the repo's creds volume), so
    a restart or re-attach `--continue`s instead of losing the conversation. If our path encoding
    ever misses claude's, we just start fresh — a safe degradation, never a broken launch.
    """
    project = config_dir / "projects" / str(cwd).replace("/", "-")
    if any(project.glob("*.jsonl")):
        return ["claude", "--continue"]  # has context; re-prefilling would re-orient mid-conversation
    return ["claude", prefill]


def _exec_claude(prefill: str) -> None:  # pragma: no cover - real LLM; skipif-gated / live only
    """Replace this process with `claude` (resumed, or fresh + prefilled), pointed at the creds."""
    argv = _claude_argv(Path(CREDS_DIR), Path.cwd(), prefill)
    os.execvpe(argv[0], argv, {**os.environ, "CLAUDE_CONFIG_DIR": CREDS_DIR})


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    home: Path | None = None,
    launch: Callable[[str], None] = _exec_claude,
) -> None:
    """Bootstrap the agent CLI from the active workflow (skills + turn-flip hooks), then launch
    the agent with a prefilled prompt."""
    env = os.environ
    client = client_factory(env["PANOPTICON_SERVICE_URL"])
    task_id = env["PANOPTICON_TASK_ID"]
    home = home or Path.home()
    render_skills(client, task_id, home)
    write_settings(home)  # turn-flip hooks (Slice 4 contract)
    launch(build_prefill(client.get_task(task_id)))


if __name__ == "__main__":  # pragma: no cover
    main()
