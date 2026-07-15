"""The in-container **agent launcher** — what the runner's tmux pane runs.

It prepares the agent CLI's surface from the active workflow (skills + turn-flip hooks), then
runs the agent. Which CLI is the task's **harness** (``PANOPTICON_HARNESS``, recorded on the
task; default claude) — all CLI-specific mechanics live behind
:class:`~panopticon.harnesses.Harness`; this launcher just fetches the workflow data, hands it
to the harness, and launches. This is the only LLM-bearing path (the determinism invariant):
the **bootstrap** is deterministic and unit-tested with fakes; the **launch** (the real CLI) is
injectable and only runs for real in a `skipif`-gated integration / a live container — never in CI.

Auth is harness-specific env/files the runner injects from the repo's secrets (ADR 0007); the
launcher only asks the harness whether they're present and fails the spawn with the harness's
own message when not.

The container's entrypoint (`python -m panopticon.container`) holds the liveness connection;
this runs alongside it in the tmux pane, so `tmux attach` reaches the live agent.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.models import Skill
from panopticon.harnesses import BootstrapContext, Harness, LaunchContext, get_harness


def _run_agent(
    harness: Harness, ctx: LaunchContext
) -> None:  # pragma: no cover - real LLM; skipif-gated / live only
    """Run the harness's CLI (resuming the session if any) in the foreground; return when it
    exits.

    Unlike an ``exec``, this returns control to :func:`main` when the agent exits, so it can stop
    the container (the task → down → respawn). The CLI inherits this pane's TTY (it's the
    interactive surface ``tmux attach`` reaches)."""
    subprocess.run(harness.argv(ctx), env={**os.environ, **harness.env(ctx)})


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
    launch: Callable[[Harness, LaunchContext], None] = _run_agent,
    on_exit: Callable[[], None] = _stop_container,
) -> None:
    """Bootstrap the task's harness from the active workflow (skills + turn-flip hooks), run the
    agent, then stop the container when it exits. The CLI's config dir is a per-task volume
    (``<home>/<harness.config_dirname>``); auth comes from the env/files the runner injects.

    When the agent exits, ``on_exit`` stops the container so the task goes **down** rather
    than lingering live-but-unconnectable — the operator respawns it with `R` (history resumes)."""
    env = os.environ
    service_url = env["PANOPTICON_SERVICE_URL"]
    client = client_factory(service_url)
    harness = get_harness(env.get("PANOPTICON_HARNESS"))
    home = home or Path.home()
    task_id = env["PANOPTICON_TASK_ID"]
    runner_id = env.get("PANOPTICON_RUNNER_ID")
    if detail := harness.missing_auth(env, home=home):
        if runner_id:
            client.report_lifecycle(task_id, runner_id, phase="failed", detail=detail)
        return
    harness.bootstrap(
        BootstrapContext(
            home=home,
            cwd=Path.cwd(),
            service_url=service_url,
            task_id=task_id,
            skills=[Skill(**s) for s in client.list_skills(task_id)],
            operations=client.list_operations(task_id),
            overview=client.workflow_overview(task_id),
            environ=env,
        )
    )
    launch(
        harness,
        LaunchContext(
            home=home,
            cwd=Path.cwd(),
            initial_prompt=env.get("PANOPTICON_INITIAL_PROMPT") or None,
            turn=env.get("PANOPTICON_TASK_TURN") or None,
            starting_model=env.get("PANOPTICON_STARTING_MODEL") or None,
        ),
    )  # the agent runs until it exits...
    on_exit()  # ...then stop the container (task → down → respawn)


if __name__ == "__main__":  # pragma: no cover
    main()
