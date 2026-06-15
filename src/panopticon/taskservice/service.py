"""The task service: deterministic orchestration over the store.

Owns the store (sole DB authority, ADR 0006), the workflow registry, the artifact
store, and ephemeral liveness registrations. All task-state mutations flow through here and
are enforced by the workflow before persistence ("transition enforcement at the boundary").

Uses a clock for timestamps and an id factory for ids; both are injectable so tests are
deterministic. No LLM (the determinism invariant).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from panopticon.core.artifacts import ArtifactStore
from panopticon.core.models import Repo, Status, Task
from panopticon.core.store import NotFound, Store
from panopticon.core.workflow import Workflow


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class UnknownWorkflow(Exception):
    """Raised when a task references a workflow the service hasn't loaded."""


@dataclass
class Registration:
    """An active container's claim that it is working on a task (liveness)."""

    id: str
    task_id: str
    container_id: str
    runner_id: str | None
    registered_at: str
    last_seen: str


class TaskService:
    def __init__(
        self,
        store: Store,
        workflows: Mapping[str, Workflow],
        artifacts: ArtifactStore,
        *,
        clock: Callable[[], str] = _utc_now_iso,
        id_factory: Callable[[], str] = _uuid_hex,
    ) -> None:
        self._store = store
        self._workflows = dict(workflows)
        self._artifacts = artifacts
        self._clock = clock
        self._id = id_factory
        self._registrations: dict[str, Registration] = {}

    # -- repos --------------------------------------------------------------------

    def create_repo(self, repo: Repo) -> Repo:
        self._store.create_repo(repo)
        return repo

    def get_repo(self, repo_id: str) -> Repo:
        repo = self._store.get_repo(repo_id)
        if repo is None:
            raise NotFound(f"repo {repo_id!r} does not exist")
        return repo

    def list_repos(self) -> list[Repo]:
        return self._store.list_repos()

    # -- workflows ----------------------------------------------------------------

    def workflow_names(self) -> list[str]:
        return sorted(self._workflows)

    def _workflow(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError:
            raise UnknownWorkflow(f"unknown workflow {name!r}") from None

    # -- tasks --------------------------------------------------------------------

    def create_task(self, repo_id: str, workflow_name: str) -> Task:
        self.get_repo(repo_id)  # ensure exists (raises NotFound)
        wf = self._workflow(workflow_name)
        task = wf.start_task(self._id(), repo_id, at=self._clock())
        self._store.create_task(task)
        return task

    def get_task(self, task_id: str) -> Task:
        task = self._store.get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r} does not exist")
        return task

    def list_tasks(self) -> list[Task]:
        return self._store.list_tasks()

    def legal_transitions(self, task_id: str) -> list[str]:
        """The states the task may move to next (its workflow's edges out of the current state)."""
        task = self.get_task(task_id)
        return sorted(self._workflow(task.workflow).transitions(task.state))

    def operations(self, task_id: str) -> dict[str, str]:
        """The named core operations available now (verb → target state) — e.g. advance/iterate/drop."""
        task = self.get_task(task_id)
        return self._workflow(task.workflow).operations(task.state)

    def apply_operation(self, task_id: str, operation: str, *, note: str | None = None) -> Task:
        """Apply a named core operation: resolve it to a transition (the verb becomes the trigger)."""
        task = self.get_task(task_id)
        wf = self._workflow(task.workflow)
        to_state = wf.resolve_operation(task.state, operation)
        wf.apply_transition(task, to_state, at=self._clock(), trigger=operation, note=note)
        self._store.save_task(task)
        return task

    def request_transition(
        self,
        task_id: str,
        to_state: str,
        *,
        trigger: str | None = None,
        note: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        wf = self._workflow(task.workflow)
        wf.apply_transition(task, to_state, at=self._clock(), trigger=trigger, note=note)
        self._store.save_task(task)
        return task

    def resolve_responsibility(
        self, task_id: str, key: str, *, status: Status, comment: str | None = None
    ) -> Task:
        """Record the agent's progress on one promised responsibility (fulfilled in place)."""
        task = self.get_task(task_id)
        task.resolve_responsibility(key=key, status=status, comment=comment)
        self._store.save_task(task)
        return task

    def set_slug(self, task_id: str, slug: str) -> Task:
        task = self.get_task(task_id)
        task.slug = slug
        self._store.save_task(task)
        return task

    # -- artifacts ----------------------------------------------------------------

    def put_artifact(self, task_id: str, name: str, content: bytes) -> None:
        self.get_task(task_id)  # ensure the task exists
        self._artifacts.put(task_id, name, content)

    def get_artifact(self, task_id: str, name: str) -> bytes | None:
        self.get_task(task_id)
        return self._artifacts.get(task_id, name)

    def list_artifacts(self, task_id: str) -> list[str]:
        self.get_task(task_id)
        return self._artifacts.list(task_id)

    # -- liveness -----------------------------------------------------------------

    def register(
        self, task_id: str, container_id: str, runner_id: str | None = None
    ) -> Registration:
        self.get_task(task_id)  # ensure the task exists
        now = self._clock()
        reg = Registration(
            id=self._id(),
            task_id=task_id,
            container_id=container_id,
            runner_id=runner_id,
            registered_at=now,
            last_seen=now,
        )
        self._registrations[reg.id] = reg
        return reg

    def heartbeat(self, registration_id: str) -> Registration:
        reg = self._registrations.get(registration_id)
        if reg is None:
            raise NotFound(f"registration {registration_id!r} does not exist")
        reg.last_seen = self._clock()
        return reg

    def deregister(self, registration_id: str) -> None:
        self._registrations.pop(registration_id, None)

    def registrations(self, task_id: str | None = None) -> list[Registration]:
        return [
            r for r in self._registrations.values() if task_id is None or r.task_id == task_id
        ]
