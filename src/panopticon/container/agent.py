"""The in-container **agent launcher** — what the runner's tmux pane runs.

It prepares the agent CLI's surface from the active workflow (skills + turn-flip hooks), links in
the repo's credentials, then `exec`s the agent. This is the only LLM-bearing path (the
determinism invariant): the **bootstrap** is deterministic and unit-tested with fakes; the
**launch** (real `claude`) is injectable and only runs for real in a `skipif`-gated integration /
a live container — never in CI.

**Config-dir layout.** ``CLAUDE_CONFIG_DIR`` is the agent CLI's *whole* state dir (settings,
rendered skills, session transcripts, …), so it must be **container-local** — pointing it at the
per-repo creds volume would share/clobber that state across every task on the repo. Only the
*credentials* are per-repo: we symlink just `.credentials.json` in from the creds volume (so the
repo's OAuth token is used and token refreshes write back to the shared, persistent volume).

The container's entrypoint (`python -m panopticon.container`) stays the liveness/heartbeat loop;
this runs alongside it in the tmux pane, so `tmux attach` reaches the live agent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from panopticon.client import TaskServiceClient
from panopticon.container.hooks import write_settings
from panopticon.container.skills import write_commands, write_operation_commands
from panopticon.core.models import Skill

#: The repo's OAuth creds volume mount inside the container (matches the runner's CREDS_MOUNT).
#: Per-repo and persistent — it holds *only* the credentials, not claude's other state.
CREDS_DIR = "/creds"
#: claude's credential file, relative to a config dir; linked in from the creds volume.
CREDS_FILE = ".credentials.json"
#: claude's main config file. Holds (besides per-container state) the logged-in *account* (which
#: claude keys "logged in" on, separately from the token in ``.credentials.json``) and per-project
#: trust acceptance.
CONFIG_FILE = ".claude.json"
#: The account/identity fields seeded from the creds volume's config into the container-local one,
#: so a task is authenticated (the token alone isn't enough — claude also wants the account).
ACCOUNT_KEYS = ("oauthAccount", "userID", "hasCompletedOnboarding", "lastOnboardingVersion")


#: Filename of the rendered MCP client config in the config dir; claude is pointed at it via
#: ``--mcp-config`` so it connects to the task service's MCP server (task operations as tools).
MCP_CONFIG_FILE = "panopticon-mcp.json"


def render_skills(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's skills to the agent CLI surface (`.claude/commands/`)."""
    skills = [Skill(**s) for s in client.list_skills(task_id)]
    return write_commands(skills, home, task_id)


def render_operations(client: TaskServiceClient, task_id: str, home: Path) -> list[Path]:
    """Render the active workflow's available core operations (advance/drop/…) as slash-commands.

    Reflects the *active workflow's* declared moves (ADR 0004), so a parity and a free-form
    container expose different operation commands — not a fixed global menu.
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


def link_credentials(config_dir: Path, *, creds_dir: Path = Path(CREDS_DIR)) -> None:
    """Point the container-local config dir at the repo's shared OAuth credentials.

    Symlink *only* ``.credentials.json`` from the per-repo creds volume into ``config_dir``, so
    the repo's token is used and refreshes write through to the persistent volume — while
    sessions/settings/skills stay container-local (not shared across the repo's tasks). Best
    effort: if the volume has no credentials yet (no `panopticon login`), leave it to claude to
    complain at launch."""
    config_dir.mkdir(parents=True, exist_ok=True)
    src, link = creds_dir / CREDS_FILE, config_dir / CREDS_FILE
    if src.exists() and not link.exists():
        link.symlink_to(src)


def trust_workspace(config_dir: Path, cwd: Path) -> Path:
    """Pre-accept claude's "Do you trust the files in this folder?" dialog for ``cwd``.

    The trust dialog is **separate** from the permission prompts ``--dangerously-skip-permissions``
    skips (cf. claude issue #45298): it blocks on startup until accepted, and there's no operator in
    the container to accept it. claude records acceptance per-project in ``<config>/.claude.json``
    under ``projects[<cwd>].hasTrustDialogAccepted``; we seed exactly that (and mark onboarding done,
    the other first-run blocker). Merge-in-place so we don't clobber config claude writes itself (nor
    the account :func:`seed_account` seeds into the same file), and idempotent. The path encoding is
    undocumented internals — a safe degradation if it ever drifts is that the dialog reappears, which
    only matters in an (already attended) interactive re-attach.
    """
    config = config_dir / CONFIG_FILE
    config.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = json.loads(config.read_text()) if config.exists() else {}
    data["hasCompletedOnboarding"] = True
    projects = data.setdefault("projects", {})
    projects.setdefault(str(cwd), {})["hasTrustDialogAccepted"] = True
    config.write_text(json.dumps(data, indent=2))
    return config


def seed_account(config_dir: Path, *, creds_dir: Path = Path(CREDS_DIR)) -> None:
    """Seed the logged-in account into the container-local config so the task is authenticated.

    The token is shared via :func:`link_credentials`, but claude also keys "logged in" on the
    account record (``oauthAccount``/``userID``) in ``.claude.json`` — which is container-local and
    fresh, so a task would have the token yet still be prompted to log in. Copy just the identity
    fields from the creds volume's ``.claude.json`` (written by ``panopticon login``); claude fills
    in the rest. Best effort: skipped if the volume has no config (never logged in) or it's
    unreadable. ``.claude.json`` itself stays container-local — only these fields are shared."""
    src = creds_dir / CONFIG_FILE
    try:
        shared = json.loads(src.read_text())
    except (OSError, ValueError):
        return
    account = {key: shared[key] for key in ACCOUNT_KEYS if key in shared}
    if not account:
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / CONFIG_FILE
    try:
        existing = json.loads(dest.read_text())
    except (OSError, ValueError):
        existing = {}
    dest.write_text(json.dumps({**existing, **account}, indent=2))


def _claude_argv(config_dir: Path, cwd: Path) -> list[str]:
    """`claude` argv, resuming the project's most recent conversation if one exists.

    The agent runs unattended in a throwaway container on a per-task clone, so it launches with
    ``--dangerously-skip-permissions`` — there's no operator to answer permission prompts, and the
    blast radius is the task's own checkout. claude keeps per-project transcripts under
    ``<config>/projects/<cwd with '/' → '-'>``; when one is there (e.g. the pane or operator
    re-attached) we ``--continue`` it instead of starting fresh. The config dir is container-local,
    so this resumes within a container's life — not across re-creation (cross-restart persistence is
    the per-task worktree, ADR 0010 §5). If our path encoding ever misses claude's, we simply start
    fresh — a safe degradation.
    """
    argv = ["claude", "--dangerously-skip-permissions"]
    mcp_config = config_dir / MCP_CONFIG_FILE
    if mcp_config.exists():  # connect to the task service's MCP server, and *only* it
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    project = config_dir / "projects" / str(cwd).replace("/", "-")
    if any(project.glob("*.jsonl")):
        argv.append("--continue")
    return argv


def _exec_claude(config_dir: Path) -> None:  # pragma: no cover - real LLM; skipif-gated / live only
    """Replace this process with `claude` (resuming the session if any), with its config dir."""
    argv = _claude_argv(config_dir, Path.cwd())
    os.execvpe(argv[0], argv, {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)})


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    home: Path | None = None,
    launch: Callable[[Path], None] = _exec_claude,
) -> None:
    """Bootstrap the agent CLI from the active workflow (skills + turn-flip hooks + credentials),
    then launch the agent. The CLI config dir is container-local (`<home>/.claude`); only the
    credentials are linked in from the per-repo creds volume."""
    env = os.environ
    service_url = env["PANOPTICON_SERVICE_URL"]
    client = client_factory(service_url)
    config_dir = (home or Path.home()) / ".claude"
    task_id = env["PANOPTICON_TASK_ID"]
    render_skills(client, task_id, config_dir.parent)
    render_operations(client, task_id, config_dir.parent)  # advance/drop/… as slash-commands
    write_settings(config_dir.parent)  # turn-flip hooks → <home>/.claude/settings.json
    write_mcp_config(config_dir, service_url)  # point claude at the task service's MCP server
    trust_workspace(config_dir, Path.cwd())  # pre-accept the trust dialog (no operator to)
    link_credentials(config_dir)
    seed_account(config_dir)  # + the logged-in account, so the token alone isn't a login prompt
    launch(config_dir)


if __name__ == "__main__":  # pragma: no cover
    main()
