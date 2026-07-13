"""The in-container **agent launcher** — what the runner's tmux pane runs.

It prepares the agent CLI's surface from the active workflow (skills + turn-flip hooks), then
`exec`s the agent. This is the only LLM-bearing path (the determinism invariant): the
**bootstrap** is deterministic and unit-tested with fakes; the **launch** (real `claude`) is
injectable and only runs for real in a `skipif`-gated integration / a live container — never in CI.

Auth is the ``CLAUDE_CODE_OAUTH_TOKEN`` env var the runner injects from the repo's ``env_file``;
the launcher does no credential wiring of its own.

The container's entrypoint (`python -m panopticon.container`) holds the liveness connection;
this runs alongside it in the tmux pane, so `tmux attach` reaches the live agent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.container.config import update_json_config
from panopticon.container.hooks import write_settings
from panopticon.container.skills import write_commands, write_operation_commands
from panopticon.core.models import Skill

#: claude's main config file. Holds (besides per-container state) per-project trust acceptance.
CONFIG_FILE = ".claude.json"


#: Filename of the rendered MCP client config in the config dir; claude is pointed at it via
#: ``--mcp-config`` so it connects to the task service's MCP server (task operations as tools).
MCP_CONFIG_FILE = "panopticon-mcp.json"
#: Filename of the rendered workflow overview (the whole-lifecycle map); claude gets its contents in
#: the system prompt via ``--append-system-prompt`` so the agent always knows the workflow's shape.
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"


def render_skills(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's skills to the agent CLI surface (`.claude/commands/`)."""
    skills = [Skill(**s) for s in client.list_skills(task_id)]
    return write_commands(skills, home, task_id)


def render_operations(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's available core operations (advance/drop/…) as slash-commands.

    Reflects the *active workflow's* declared moves (ADR 0004), so a github-peer-reviewed and a
    free-form container expose different operation commands — not a fixed global menu.
    """
    return write_operation_commands(client.list_operations(task_id), home, task_id)


def write_mcp_config(config_dir: Path, service_url: str) -> Path:
    """Write claude's MCP client config so it connects to the task service's MCP server.

    A single ``panopticon`` HTTP server at ``<service_url>/mcp`` — the same control plane the
    container already polls (``PANOPTICON_SERVICE_URL``, the in-container view). Returns the path,
    which the launcher passes to ``claude --mcp-config``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / MCP_CONFIG_FILE
    server = {"type": "http", "url": f"{service_url.rstrip('/')}/mcp"}
    path.write_text(json.dumps({"mcpServers": {"panopticon": server}}, indent=2))
    return path


def write_workflow_overview(config_dir: Path, overview: str) -> Path | None:
    """Write the whole-workflow map so the launcher can put it in claude's system prompt. Returns the
    path, or ``None`` when there's no overview (skipped — the agent just gets the per-turn briefing)."""
    if not overview.strip():
        return None
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / WORKFLOW_OVERVIEW_FILE
    path.write_text(overview)
    return path


def trust_workspace(config_dir: Path, cwd: Path) -> Path:
    """Pre-accept claude's first-run dialogs for ``cwd``.

    Three blockers fire on a fresh container and must be pre-seeded — there is no operator in the
    container to dismiss them interactively:

    - ``hasCompletedOnboarding`` — the general onboarding screen.
    - ``projects[<cwd>].hasTrustDialogAccepted`` — "Do you trust the files in this folder?"
      (cf. claude issue #45298; separate from ``--dangerously-skip-permissions``).
    - ``hasAcknowledgedCostThreshold`` — cost-acknowledgment dialog shown when authenticating
      via ``ANTHROPIC_API_KEY`` (not shown for OAuth tokens).

    Merge-in-place so we don't clobber config claude writes itself, and idempotent. The path
    encoding is undocumented internals — a safe degradation if it ever drifts is that the dialog
    reappears, which only matters in an (already attended) interactive re-attach.
    """
    config = config_dir / CONFIG_FILE
    with update_json_config(config) as data:
        data["hasCompletedOnboarding"] = True
        data["hasAcknowledgedCostThreshold"] = True
        projects = data.setdefault("projects", {})
        projects.setdefault(str(cwd), {})["hasTrustDialogAccepted"] = True
    return config


#: Sent to claude as the first message when a container restarts mid-task on the agent's turn.
INTERRUPT_PROMPT = "You were interrupted. Continue."


def _claude_argv(
    config_dir: Path,
    cwd: Path,
    *,
    initial_prompt: str | None = None,
    turn: str | None = None,
    starting_model: str | None = None,
) -> list[str]:
    """`claude` argv, resuming the project's most recent conversation if one exists.

    The agent runs unattended in a throwaway container on a per-task clone, so it launches with
    ``--dangerously-skip-permissions`` — there's no operator to answer permission prompts, and the
    blast radius is the task's own checkout. claude keeps per-project transcripts under
    ``<config>/projects/<cwd with '/' → '-'>``; when one is there we ``--continue`` it instead of
    starting fresh. The config dir is a **per-task volume** (the runner mounts it; ``CONFIG_MOUNT``),
    so this resumes both within a container's life and **across respawn/recreate** — claude history
    persists even though the container layer is thrown away. If our path encoding ever misses
    claude's, we simply start fresh — a safe degradation.

    On a **first run** (no prior session) with an ``initial_prompt``, the prompt is appended as a
    positional argument so claude processes it immediately. On a **resumed session** (``--continue``)
    the ``initial_prompt`` is omitted — the agent is already mid-task. When the resumed session is
    the agent's turn (``turn == "agent"``), :data:`INTERRUPT_PROMPT` is appended instead so the
    agent automatically picks up where it left off rather than waiting for user input.

    ``starting_model`` (e.g. ``"opus"``) is passed as ``--model`` on the **first run only** — on
    resume claude uses whichever model the conversation was already using.
    """
    argv = ["claude", "--dangerously-skip-permissions"]
    overview = config_dir / WORKFLOW_OVERVIEW_FILE
    if overview.exists():  # the whole-workflow map → claude's system prompt (so it knows the shape)
        argv += ["--append-system-prompt", overview.read_text()]
    mcp_config = config_dir / MCP_CONFIG_FILE
    if mcp_config.exists():  # connect to the task service's MCP server, and *only* it
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    project = config_dir / "projects" / str(cwd).replace("/", "-")
    if any(project.glob("*.jsonl")):
        argv.append("--continue")
        if turn == "agent":
            argv.append(INTERRUPT_PROMPT)  # positional: auto-resume after container restart
    else:
        if (
            starting_model
        ):  # first run only — on resume claude uses the conversation's existing model
            argv += ["--model", starting_model]
        if initial_prompt:
            argv.append(
                initial_prompt
            )  # positional: claude sends this as the agent's first message
    return argv


def _run_claude(config_dir: Path) -> None:  # pragma: no cover - real LLM; skipif-gated / live only
    """Run `claude` (resuming the session if any) in the foreground; return when it exits.

    Unlike an ``exec``, this returns control to :func:`main` when claude exits, so it can stop the
    container (the task → down → respawn). claude inherits this pane's TTY (it's the interactive
    surface ``tmux attach`` reaches)."""
    initial_prompt = os.environ.get("PANOPTICON_INITIAL_PROMPT") or None
    turn = os.environ.get("PANOPTICON_TASK_TURN") or None
    starting_model = os.environ.get("PANOPTICON_STARTING_MODEL") or None
    argv = _claude_argv(
        config_dir,
        Path.cwd(),
        initial_prompt=initial_prompt,
        turn=turn,
        starting_model=starting_model,
    )
    subprocess.run(argv, env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)})


def _stop_container() -> None:  # pragma: no cover - signals the real container's PID 1
    """Stop the container by signalling the entrypoint (PID 1, the liveness connection). Both it and this
    launcher run as the same unprivileged user, so the signal is permitted; PID 1 deregisters and
    exits on SIGTERM, so the container stops → the task shows **down** → the operator respawns (`R`),
    resuming from the per-task config volume."""
    os.kill(1, signal.SIGTERM)


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    home: Path | None = None,
    launch: Callable[[Path], None] = _run_claude,
    on_exit: Callable[[], None] = _stop_container,
) -> None:
    """Bootstrap the agent CLI from the active workflow (skills + turn-flip hooks), run the agent,
    then stop the container when it exits. The CLI config dir is a per-task volume
    (`<home>/.claude`); auth comes from the ``CLAUDE_CODE_OAUTH_TOKEN`` env var the runner injects.

    When the agent (claude) exits, ``on_exit`` stops the container so the task goes **down** rather
    than lingering live-but-unconnectable — the operator respawns it with `R` (history resumes)."""
    env = os.environ
    service_url = env["PANOPTICON_SERVICE_URL"]
    client = client_factory(service_url)
    config_dir = (home or Path.home()) / ".claude"
    task_id = env["PANOPTICON_TASK_ID"]
    runner_id = env.get("PANOPTICON_RUNNER_ID")
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
        if runner_id:
            client.report_lifecycle(
                task_id,
                runner_id,
                phase="failed",
                detail="No auth token — set CLAUDE_CODE_OAUTH_TOKEN in the repo's env_file (see docs/container-auth.md)",
            )
        return
    render_skills(client, task_id, config_dir.parent)
    render_operations(client, task_id, config_dir.parent)  # advance/drop/… as slash-commands
    write_settings(config_dir.parent)  # turn-flip hooks → <home>/.claude/settings.json
    write_mcp_config(config_dir, service_url)  # point claude at the task service's MCP server
    write_workflow_overview(
        config_dir, client.workflow_overview(task_id)
    )  # → system prompt (the map)
    trust_workspace(config_dir, Path.cwd())  # pre-accept the trust dialog (no operator to)
    launch(config_dir)  # the agent runs until it exits...
    on_exit()  # ...then stop the container (task → down → respawn)


if __name__ == "__main__":  # pragma: no cover
    main()
