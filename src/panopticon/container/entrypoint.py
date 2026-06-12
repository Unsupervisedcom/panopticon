"""The container entrypoint protocol (skeleton form).

A real task container will run an agent (the only place LLMs run). Here we implement the
deterministic *protocol* the entrypoint owns, so it can be exercised without Docker:

1. connect to the task service and **register** (liveness) — and stay registered until done;
2. if the task has no **slug**, set one (the slug hook — slugs are decided in the container,
   unlike cloude-cade, per ARCHITECTURE.md §8.3);
3. run the task's work (here, an injected callback stands in for the agent);
4. deregister on exit.
"""

from __future__ import annotations

from collections.abc import Callable

from panopticon.container.client import TaskServiceClient

Work = Callable[[TaskServiceClient, str], None]


def run_task_container(
    client: TaskServiceClient,
    task_id: str,
    *,
    container_id: str,
    runner_id: str | None = None,
    proposed_slug: str | None = None,
    work: Work | None = None,
) -> None:
    """Run the entrypoint protocol for ``task_id`` against the task service."""
    registration = client.register(task_id, container_id=container_id, runner_id=runner_id)
    try:
        task = client.get_task(task_id)
        if task["slug"] is None and proposed_slug is not None:
            client.set_slug(task_id, proposed_slug)  # the slug hook
        client.heartbeat(registration["id"])
        if work is not None:
            work(client, task_id)
    finally:
        client.deregister(registration["id"])
