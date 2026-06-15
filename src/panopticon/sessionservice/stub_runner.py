"""A stub runner for the walking skeleton.

Stands in for the session service: instead of spawning a container on the host Docker
daemon and a tmux session (ADR 0008), it runs the container entrypoint **in-process**, so
the end-to-end path works without Docker. Real adapters replace this behind the same idea.
"""

from __future__ import annotations

import itertools

from panopticon.container.client import TaskServiceClient
from panopticon.container.entrypoint import Work, run_task_container
from panopticon.sessionservice.runner import Runner


class StubRunner(Runner):
    def __init__(self, client: TaskServiceClient, *, runner_id: str = "stub-runner") -> None:
        self._client = client
        self._runner_id = runner_id
        self._counter = itertools.count(1)

    def spawn(
        self, task_id: str, *, proposed_slug: str | None = None, work: Work | None = None
    ) -> str:
        """"Spawn" a fake container for ``task_id`` and return its container id.

        Runs the entrypoint protocol in-process. ``proposed_slug``/``work`` are skeleton-only
        affordances (a real runner passes neither — the container decides its slug and runs the
        agent); they extend, not replace, the :class:`Runner` contract.
        """
        container_id = f"{self._runner_id}-c{next(self._counter)}"
        run_task_container(
            self._client,
            task_id,
            container_id=container_id,
            runner_id=self._runner_id,
            proposed_slug=proposed_slug,
            work=work,
        )
        return container_id

    def stop(self, container_id: str) -> None:
        """No-op: the in-process entrypoint already ran to completion (and deregistered)."""
