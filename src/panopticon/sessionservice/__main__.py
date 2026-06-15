"""``python -m panopticon.sessionservice <task_id>`` — spawn one task container.

The minimal runnable form of the runner host process: spawn a container for a given task
against a task service. (A daemon that *pulls* assigned work arrives with the assignment
protocol in a later slice; this is the underlying primitive it will call.)
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import httpx

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import (
    DEFAULT_IMAGE,
    CommandRunner,
    LocalRunner,
    _subprocess_run,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    run: CommandRunner = _subprocess_run,
    client: TaskServiceClient | None = None,
) -> str:
    parser = argparse.ArgumentParser(
        prog="python -m panopticon.sessionservice", description="Spawn a task container."
    )
    parser.add_argument("task_id")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PANOPTICON_SERVICE_URL", "http://host.docker.internal:8000"),
        help="task service URL the container connects back to",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args(argv)

    # Look up the task's repo to inject that repo's secrets (ADR 0007), scoped to this task.
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    repo = client.get_repo(client.get_task(args.task_id)["repo_id"])

    container_id = LocalRunner(args.service_url, image=args.image, run=run).spawn(
        args.task_id, env_file=repo.get("env_file"), creds_volume=repo.get("creds_volume")
    )
    print(container_id)
    return container_id


if __name__ == "__main__":
    main()
