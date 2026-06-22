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
from panopticon.core.briefing import render_state_briefing, render_workflow_overview
from panopticon.core.models import Actor, Repo, Skill, Status, Task
from panopticon.core.provisioning import PROVISION_SKILL
from panopticon.core.store import NotFound, Store
from panopticon.core.workflow import Workflow


#: A registration is considered live only if heartbeated within this window (the container
#: heartbeats every ~5s; a few missed beats means it's gone). Past it, the registration is reaped
#: so a container that died without deregistering doesn't show as "live" forever.
LIVENESS_TTL_SECONDS = 20.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class UnknownWorkflow(Exception):
    """Raised when a task references a workflow the service hasn't loaded."""


class AlreadyClaimed(Exception):
    """Raised when a task is claimed by a different runner than the one claiming."""


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

    def workflow_image_layer(self, name: str) -> str:
        """The workflow's Docker image layer (ADR 0005) — the Dockerfile fragment the runner
        composes onto the base image (e.g. github-peer-reviewed's `gh`). Empty when the workflow needs none."""
        return self._workflow(name).image_layer()

    def _workflow(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError:
            raise UnknownWorkflow(f"unknown workflow {name!r}") from None

    # -- tasks --------------------------------------------------------------------

    def create_task(
        self, repo_id: str, workflow_name: str, *, description: str | None = None
    ) -> Task:
        self.get_repo(repo_id)  # ensure exists (raises NotFound)
        wf = self._workflow(workflow_name)
        task = wf.start_task(self._id(), repo_id, at=self._clock(), description=description)
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

    def workflow_states(self, task_id: str) -> list[str]:
        """Every state of the task's workflow — the candidates for a free state-set (set_state)."""
        task = self.get_task(task_id)
        return list(self._workflow(task.workflow).labels())

    def operations(self, task_id: str) -> dict[str, str]:
        """The named core operations available now (verb → target state) — advance/drop."""
        task = self.get_task(task_id)
        return self._workflow(task.workflow).operations(task.state)

    def skills(self, task_id: str) -> list[Skill]:
        """The in-container skills for a task: the agnostic `provision` skill (every task names
        itself to get a branch, ADR 0011) followed by the active workflow's own skills."""
        task = self.get_task(task_id)
        return [PROVISION_SKILL, *self._workflow(task.workflow).skills()]

    def briefing(self, task_id: str) -> str:
        """A short briefing on the task's current phase (state + responsibilities + how it advances),
        rendered from the workflow so the in-container agent knows *where it is* (the hook emits it)."""
        task = self.get_task(task_id)
        return render_state_briefing(self._workflow(task.workflow), task)

    def workflow_overview(self, task_id: str) -> str:
        """A one-time map of the task's whole workflow (the agent gets this in its system prompt)."""
        task = self.get_task(task_id)
        return render_workflow_overview(self._workflow(task.workflow))

    def apply_operation(self, task_id: str, operation: str, *, note: str | None = None) -> Task:
        """Apply a named core operation (advance/drop) — a gated move along the declared graph."""
        task = self.get_task(task_id)
        to_state = self._workflow(task.workflow).resolve_operation(task.state, operation)
        return self.request_transition(task_id, to_state, trigger=operation, note=note)

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
        return self._commit_transition(task, wf, to_state, force=False, trigger=trigger, note=note)

    def set_state(self, task_id: str, to_state: str, *, note: str | None = None) -> Task:
        """The user's free override: move the task to any state, bypassing the graph and the gate."""
        task = self.get_task(task_id)
        wf = self._workflow(task.workflow)
        return self._commit_transition(task, wf, to_state, force=True, trigger="set-state", note=note)

    def _commit_transition(
        self, task: Task, wf: Workflow, to_state: str, *, force: bool, trigger: str | None, note: str | None
    ) -> Task:
        from_state = task.state
        if force:
            wf.force_transition(task, to_state, at=self._clock(), trigger=trigger, note=note)
        else:
            wf.apply_transition(task, to_state, at=self._clock(), trigger=trigger, note=note)
        # Deterministic lifecycle hook (e.g. seed the plan on plan acceptance) — may touch the
        # task/artifacts; run before the single save so any task mutation persists with it.
        wf.on_transition(task, from_state=from_state, to_state=task.state, artifacts=self._artifacts)
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

    def set_turn(self, task_id: str, turn: Actor) -> Task:
        """Flip who holds the turn within a state (the in-container hooks' callback).

        This is the agnostic agent↔user ball tracking (ADR 0004). It leaves ``blocked``
        untouched, so a deliberate block survives turn flips.
        """
        task = self.get_task(task_id)
        task.turn = turn
        self._store.save_task(task)
        return task

    def set_blocked(self, task_id: str, blocked: bool) -> Task:
        """Set/clear the task's deliberate ``blocked`` marker (orthogonal to the turn)."""
        task = self.get_task(task_id)
        task.blocked = blocked
        self._store.save_task(task)
        return task

    # -- claim (a runner owns the task; the spawn gate, ADR 0008) --------------------------

    def claim(self, task_id: str, runner_id: str) -> Task:
        """Claim an unclaimed task for ``runner_id`` (a session service claims before spawning).

        Compare-and-set: succeeds if the task is unclaimed (idempotent if this runner already holds
        it); raises :class:`AlreadyClaimed` if a different runner does. The store is the single
        writer, so the check-and-set is serialized.
        """
        task = self.get_task(task_id)
        if task.claimed_by not in (None, runner_id):
            raise AlreadyClaimed(f"task {task_id!r} is already claimed by {task.claimed_by!r}")
        task.claimed_by = runner_id
        self._store.save_task(task)
        return task

    def release(self, task_id: str) -> Task:
        """Release a task's claim (back to unclaimed) so it can be re-claimed / respawned."""
        task = self.get_task(task_id)
        task.claimed_by = None
        self._store.save_task(task)
        return task

    # -- provisioning (the session service does the host git; the service only records) ---

    def record_provisioning(self, task_id: str, *, branch: str, clone: str) -> Task:
        """Record the slug-named branch + per-task clone the session service created **on the
        host** for this task (ADR 0010/0011 / ARCHITECTURE §9).

        The git itself happens on the runner's host (`core/git.py`), observed via the work-pull
        loop; the task service never touches a filesystem, so this stays correct when the runner
        is remote (ADR 0009). Slug-gated: the branch is named from the slug, so we refuse to
        record before one is set.

        This is a pure recorded-fact write — it does **not** run ``Workflow.provision``. ADR 0010
        §1 moves provisioning's host-touching work to the session service and leaves the
        host-side-vs-recorded-fact split of that hook an open question; until it's designed (and a
        workflow needs it), ``Workflow.provision`` stays a declared seam, unwired here.
        """
        task = self.get_task(task_id)
        if task.slug is None:
            raise ValueError("cannot record provisioning before the task's slug is set")
        task.branch = branch
        task.clone = clone
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

    def _stale(self, reg: Registration) -> bool:
        """Whether a registration has gone too long without a heartbeat — its container died without
        deregistering (SIGKILL / ``docker rm --force`` / crash). Defensive: a non-timestamp clock
        (tests) never expires, so liveness behaviour is unchanged there."""
        try:
            age = (datetime.fromisoformat(self._clock()) - datetime.fromisoformat(reg.last_seen))
        except ValueError:
            return False
        return age.total_seconds() > LIVENESS_TTL_SECONDS

    def registrations(self, task_id: str | None = None) -> list[Registration]:
        # Reap stale registrations first, so liveness reflects reality — a container that died
        # without deregistering otherwise lingers as "live" forever (no heartbeat to age it out).
        for rid in [r.id for r in self._registrations.values() if self._stale(r)]:
            del self._registrations[rid]
        return [
            r for r in self._registrations.values() if task_id is None or r.task_id == task_id
        ]
