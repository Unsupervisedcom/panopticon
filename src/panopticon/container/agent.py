"""The in-container **agent launcher** — what the runner's tmux pane runs.

It prepares the agent CLI's surface from the active workflow (skills + turn-flip hooks), then
launches the CLI. This is the only LLM-bearing path (the determinism invariant): the **bootstrap**
is deterministic and unit-tested with fakes; the **launch** (the real CLI) is the adapter's
:meth:`~panopticon.container.cli.AgentCLI.launch`, injectable and only run for real in a
`skipif`-gated integration / a live container — never in CI.

The launcher is **CLI-agnostic** (ADR 0014): it resolves an :class:`~panopticon.container.cli.AgentCLI`
adapter from the CLI name the runner passes (``PANOPTICON_AGENT_CLI``, defaulting to ``claude``) and
drives the bootstrap-then-launch sequence against it, holding no ``claude`` literal. Auth is the
adapter's env-var check plus its :meth:`~panopticon.container.cli.AgentCLI.write_credentials` step
(claude reads its token from the env; codex materializes ``auth.json``) — the launcher itself knows
no CLI-specific credential shape.

The container's entrypoint (`python -m panopticon.container`) holds the liveness connection;
this runs alongside it in the tmux pane, so `tmux attach` reaches the live agent.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.container.cli import AgentCLI, get_agent_cli


def _stop_container() -> None:  # pragma: no cover - signals the real container's PID 1
    """Stop the container by signalling the entrypoint (PID 1, the liveness connection). Both it and
    this launcher run as the same unprivileged user, so the signal is permitted; PID 1 deregisters
    and exits on SIGTERM, so the container stops → the task shows **down** → the operator respawns
    (`R`), resuming from the per-task config volume."""
    os.kill(1, signal.SIGTERM)


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    home: Path | None = None,
    agent_cli: AgentCLI | None = None,
    launch: Callable[[Path], None] | None = None,
    on_exit: Callable[[], None] = _stop_container,
) -> None:
    """Bootstrap the agent CLI from the active workflow (skills + turn-flip hooks), run the agent,
    then stop the container when it exits. The adapter is resolved from ``PANOPTICON_AGENT_CLI``
    (default ``claude``); its config dir is a per-task volume (`<home>/<config_dirname>`), and auth
    comes from the adapter's env-var check (the runner injects it from the repo's ``env_file``).

    When the agent exits, ``on_exit`` stops the container so the task goes **down** rather than
    lingering live-but-unconnectable — the operator respawns it with `R` (history resumes)."""
    env = os.environ
    cli = agent_cli or get_agent_cli(env.get("PANOPTICON_AGENT_CLI"))
    service_url = env["PANOPTICON_SERVICE_URL"]
    client = client_factory(service_url)
    config_dir = (home or Path.home()) / cli.config_dirname
    task_id = env["PANOPTICON_TASK_ID"]
    runner_id = env.get("PANOPTICON_RUNNER_ID")
    detail = cli.auth_missing_detail(env, config_dir)
    if detail is not None:
        if runner_id:
            client.report_lifecycle(task_id, runner_id, phase="failed", detail=detail)
        return
    cli.render_skills(client, task_id, config_dir.parent)
    cli.render_operations(client, task_id, config_dir.parent)  # advance/drop/… as slash-commands
    cli.write_settings(config_dir.parent)  # wire turn-flip hooks where the CLI supports them
    cli.write_mcp_config(config_dir, service_url)  # point the CLI at the task service's MCP server
    cli.write_workflow_overview(
        config_dir, client.workflow_overview(task_id)
    )  # → the agent's context (the map)
    cli.trust_workspace(config_dir, Path.cwd())  # pre-accept the trust dialog (no operator to)
    cli.write_credentials(
        config_dir, env
    )  # materialize on-disk creds (codex auth.json; claude no-op)
    (launch or cli.launch)(config_dir)  # the agent runs until it exits...
    on_exit()  # ...then stop the container (task → down → respawn)


if __name__ == "__main__":  # pragma: no cover
    main()
