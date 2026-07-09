"""``python -m panopticon.sessionservice <task_id>`` — spawn one task container.

The minimal runnable form of the runner host process: spawn a container for a given task
against a task service. (A daemon that *pulls* assigned work arrives with the assignment
protocol in a later slice; this is the underlying primitive it will call.)
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.core.dirs import user_cache_dir, user_data_dir
from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.local_runner import (
    DEFAULT_IMAGE,
    CommandRunner,
    LocalRunner,
    _subprocess_run,
)
from panopticon.sessionservice.spawn import prepare_workspace

#: Per-host provisioning roots (ADR 0010/0011): the per-repo clone cache and the per-task clones.
DEFAULT_CLONE_CACHE_ROOT: str = str(user_cache_dir() / "repos")
DEFAULT_TASKS_ROOT: str = str(user_data_dir() / "tasks")


def _migrate_session_dirs(clone_cache_root: str, tasks_root: str) -> None:
    """Migrate legacy cache/tasks dirs to XDG locations.

    Tries CWD-relative paths (pre-#251) then ``~/.panopticon/`` (#251) as sources.
    Skips when a custom override is in use or the destination already exists.
    """
    import logging
    import shutil

    if clone_cache_root == DEFAULT_CLONE_CACHE_ROOT:
        new = Path(clone_cache_root)
        if not new.exists():
            for old in [Path("cache"), Path.home() / ".panopticon" / "cache"]:
                if old.is_dir():
                    logging.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break

    if tasks_root == DEFAULT_TASKS_ROOT:
        new = Path(tasks_root)
        if not new.exists():
            for old in [Path("tasks"), Path.home() / ".panopticon" / "tasks"]:
                if old.is_dir():
                    logging.info("panopticon: migrating %s → %s", old.resolve(), new)
                    new.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(new))
                    break


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
    parser.add_argument("--cache-root", default=os.environ.get("PANOPTICON_CACHE_ROOT", DEFAULT_CLONE_CACHE_ROOT))
    parser.add_argument("--tasks-root", default=os.environ.get("PANOPTICON_TASKS_ROOT", DEFAULT_TASKS_ROOT))
    args = parser.parse_args(argv)
    _migrate_session_dirs(args.cache_root, args.tasks_root)

    # Look up the task's repo to inject that repo's secrets (ADR 0007), scoped to this task.
    client = client or TaskServiceClient(httpx.Client(base_url=args.service_url))
    repo = client.get_repo(client.get_task(args.task_id)["repo_id"])

    # Spawn-prep (ADR 0011): give the task a writable per-task clone, mounted at /workspace.
    workspace = prepare_workspace(
        args.task_id, repo,
        cache=CloneCache(args.cache_root, run=run), tasks_root=args.tasks_root, git=GitClones(run=run),
    )
    container_id = LocalRunner(args.service_url, image=args.image, run=run).spawn(
        args.task_id,
        env_file=repo.get("env_file"), workspace=workspace,
    )
    print(container_id)
    return container_id


if __name__ == "__main__":
    main()
