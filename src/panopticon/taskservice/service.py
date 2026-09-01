"""The task service: deterministic orchestration over the store.

Owns the store (sole DB authority, ADR 0006), the workflow registry, the artifact
store, and ephemeral liveness registrations. All task-state mutations flow through here and
are enforced by the workflow before persistence ("transition enforcement at the boundary").

Uses a clock for timestamps and an id factory for ids; both are injectable so tests are
deterministic. No LLM (the determinism invariant).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from panopticon.core.artifacts import ArtifactStore, decode_b64_artifact
from panopticon.core.dirs import secrets_file_path
from panopticon.core.layers import LayerStore
from panopticon.core.models import (
    Actor,
    Ask,
    AskStatus,
    ContainerStatus,
    LifecyclePhase,
    Repo,
    Skill,
    Status,
    Task,
    compose_container_status,
)
from panopticon.core.provisioning import PROVISION_SKILL
from panopticon.core.state import TERMINAL_LABELS, Dropped
from panopticon.core.store import NotFound, Store
from panopticon.core.workflow import Workflow
from panopticon.taskservice.tarot_gate import (
    RESPONSIBILITY_KEY as TAROT_RESPONSIBILITY_KEY,
)
from panopticon.taskservice.tarot_gate import (
    TarotGate,
    TarotGateRefused,
    authoring_skill,
)

_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


#: Max unanswered asks (PENDING or DELIVERED) a task may hold at once (ask-the-author). A reviewer
#: may queue several questions; delivery stays strictly serialized (one delivered-unanswered at a
#: time). Only a **full** queue is rejected — a small bound so a runaway loop can't grow unboundedly.
ASK_QUEUE_CAP = 10


class UnknownWorkflow(Exception):
    """Raised when a task references a workflow the service hasn't loaded."""


class AlreadyClaimed(Exception):
    """Raised when a task is claimed by a different runner than the one claiming."""


class NotAuthorized(Exception):
    """Raised when a task attempts an operation its workflow isn't permitted (e.g. a
    non-orchestration workflow trying to create other tasks)."""


class AskQueueFull(Exception):
    """Raised when a task's ask queue is at capacity (:data:`ASK_QUEUE_CAP` unanswered asks)."""


class AskGone(Exception):
    """Raised when reading an ask whose task's config volume was reaped — undeliverable (→ 410)."""


@dataclass
class Registration:
    """An active container's claim that it is working on a task (liveness).

    A registration exists for exactly as long as the container holds its liveness connection open
    (the ``/live`` stream): the connection *is* the signal. There is no heartbeat and no
    ``last_seen`` — death is detected by the connection dropping (see :meth:`TaskService.register`
    / :meth:`deregister` and the ``/live`` endpoint), not by aging out a timestamp."""

    id: str
    task_id: str
    container_id: str
    runner_id: str | None
    registered_at: str


@dataclass
class ContainerLifecycle:
    """The session service's latest reported spawn phase for a task (ADR 0008 feedback).

    Ephemeral, like :class:`Registration`: the runner pushes it over ``PUT /tasks/{id}/lifecycle``
    as it claims → prepares → builds → starts the container, and it's cleared on claim release /
    reclaim (a respawn starts clean). Not persisted — it's transient runtime state, re-reported on
    the next spawn pass. Folded with registration presence + runner liveness into the displayed
    :class:`~panopticon.core.models.ContainerStatus` (see :meth:`TaskService.container_status`)."""

    task_id: str
    runner_id: str
    phase: LifecyclePhase
    detail: str | None
    at: str


@dataclass
class RunnerRegistration:
    """A session-service (runner) host's standing signal that it is alive and managing its tasks.

    The host-liveness counterpart of a container :class:`Registration`, one layer up: it exists for
    exactly as long as the runner holds its ``/runners/{id}/live`` connection open — the connection
    *is* the signal. The daemon dying (clean stop or crash) drops it, and the runner falls out of
    :meth:`TaskService.live_runners`; no heartbeat, no ``last_seen``, no TTL. Each connection gets a
    fresh ``id`` (not keyed by ``runner_id``) so an overlapping reconnect during a blip can't have
    the *old* connection's disconnect reap the *new* one."""

    id: str
    runner_id: str
    registered_at: str
    host: str | None = None  # the runner's hostname or operator alias (M5: remote attach)


class TaskService:
    def __init__(
        self,
        store: Store,
        workflows: Mapping[str, Workflow],
        artifacts: ArtifactStore,
        *,
        layers: LayerStore | None = None,
        clock: Callable[[], str] = _utc_now_iso,
        id_factory: Callable[[], str] = _uuid_hex,
        tarot_gate: TarotGate | None = None,
    ) -> None:
        self._store = store
        self._workflows = dict(workflows)
        self._artifacts = artifacts
        self._layers = layers
        self._clock = clock
        self._id = id_factory
        #: The review-artifact gate (host-side `tarot`). Always present — it no-ops for every repo
        #: that hasn't opted in, so there's no conditional wiring to get wrong.
        self._tarot_gate = tarot_gate if tarot_gate is not None else TarotGate()
        self._registrations: dict[str, Registration] = {}
        self._runner_registrations: dict[str, RunnerRegistration] = {}
        self._lifecycles: dict[str, ContainerLifecycle] = {}
        #: Ephemeral ask-the-author records (question → delivery → answer), keyed by ask id. Held in
        #: memory like registrations/lifecycles — a review-time conversation, not stored task state,
        #: so it never bumps the store version (ephemeral changes wake the feed via ``_notify_change``).
        self._asks: dict[str, Ask] = {}
        # Ephemeral liveness (registrations, runner liveness, lifecycle phases) lives outside the
        # store, so it doesn't bump the store's version. But the dashboard's change-feed long-poll
        # only wakes on a version change — so a container going live or a phase advancing wouldn't
        # show until an unrelated task mutation. This epoch + listener fan-out folds those ephemeral
        # events into the same feed (``tasks_version`` adds it; ``_notify_change`` fires listeners).
        self._ephemeral_epoch = 0
        self._change_listeners: list[Callable[[], None]] = []

    async def init(self) -> None:
        """Bootstrap the store's schema (idempotent). Called by the task service's lifespan."""
        await self._store.init()

    # -- repos --------------------------------------------------------------------

    async def create_repo(self, repo: Repo) -> Repo:
        await self._validate_env_file(repo.env_file)
        await self._store.create_repo(repo)
        return repo

    async def _validate_env_file(self, env_file: str | None) -> None:
        """Reject a repo whose secrets-file reference points at a missing file.

        ``env_file`` is a *name* relative to the secrets dir (``$PANOPTICON_CONFIG/secrets``,
        ADR 0007 / #291); the runner resolves it against its own local secrets dir at
        ``docker run --env-file``. Validated here on create/update so a bad reference is caught at
        registration rather than surfacing as an obscure ``--env-file`` failure at spawn. ``None``
        (no secrets file) is valid. Raises :class:`ValueError` — for a name that escapes the
        secrets dir (via :func:`secrets_file_path`) or one that resolves to a missing file — which
        the API maps to HTTP 400.

        NOTE(M5): ``env_file`` is resolved against *this host's* secrets dir. Today the task
        service and the runner share a host (M1), so this stat answers the real question; when the
        runner is remote, the file lives on the runner's host — move/duplicate the check on the
        session service then.
        """
        path = secrets_file_path(env_file)  # None for no reference; raises ValueError on escape
        if path is None:
            return
        if not await asyncio.to_thread(os.path.isfile, path):
            raise ValueError(f"env_file {env_file!r} does not exist under the secrets dir")

    async def get_repo(self, repo_id: str) -> Repo:
        repo = await self._store.get_repo(repo_id)
        if repo is None:
            raise NotFound(f"repo {repo_id!r} does not exist")
        return repo

    async def list_repos(self) -> list[Repo]:
        return await self._store.list_repos()

    async def update_repo(self, repo_id: str, changes: Mapping[str, Any]) -> Repo:
        """Apply a partial update to a repo: merge ``changes`` onto the stored repo and persist.

        Read-modify-write, so any field not in ``changes`` (e.g. ``image_layer_file`` /
        ``capabilities``, which the dashboard never sends) is preserved. ``id`` is the key and
        can't be reassigned. Raises :class:`NotFound` if the repo is unknown.
        """
        existing = await self.get_repo(repo_id)  # raises NotFound
        if "id" in changes and changes["id"] != repo_id:
            raise ValueError("a repo's id cannot be changed")
        updated = replace(existing, **{k: v for k, v in changes.items() if k != "id"})
        if "env_file" in changes:  # validate only when the caller is actually setting the field,
            await self._validate_env_file(
                updated.env_file
            )  # so an unrelated patch never fails on it
        await self._store.update_repo(updated)
        return updated

    async def repo_image_layer(self, repo_id: str) -> str:
        """The repo's Dockerfile layer (ADR 0005's repo tier), read from its referenced file.

        ``Repo.image_layer_file`` is a file name resolved relative to the configured layers
        directory; this reads its content so the runner can compose it over REST (mirroring
        :meth:`workflow_image_layer`). Empty string when the repo declares no layer. Raises
        :class:`NotFound` when a referenced file is configured but absent (or no layer store is
        wired), and the layer store rejects a name that escapes its root.
        """
        name = (
            await self.get_repo(repo_id)
        ).image_layer_file  # raises NotFound for an unknown repo
        if not name:
            return ""
        if self._layers is None:
            raise NotFound(f"no layer store configured to read image layer {name!r}")
        content = await self._layers.get(name)
        if content is None:
            raise NotFound(f"image layer file {name!r} not found")
        return content.decode()

    # -- workflows ----------------------------------------------------------------

    async def workflow_names(self) -> list[str]:
        return sorted(self._workflows)

    async def list_workflow_infos(self) -> list[dict[str, str | bool]]:
        """Each workflow's name, when_to_use description and opt_in flag, sorted by name.
        ``hidden`` workflows are omitted — this drives the repo form's enable/disable menu."""
        return [
            {
                "name": name,
                "when_to_use": self._workflows[name].when_to_use,
                "opt_in": self._workflows[name].opt_in,
            }
            for name in sorted(self._workflows)
            if not self._workflows[name].hidden
        ]

    async def list_workflow_infos_for_repo(self, repo_id: str) -> list[dict[str, str | bool]]:
        """Workflows visible for a repo, filtered by opt_in and the repo's workflow preferences.
        ``hidden`` workflows are omitted — this drives the task-creation picker (a hidden workflow
        stays creatable via the API / a dedicated hotkey; ``hidden`` is display-only, not a gate)."""
        repo = await self.get_repo(repo_id)
        return [
            {
                "name": name,
                "when_to_use": self._workflows[name].when_to_use,
                "opt_in": self._workflows[name].opt_in,
            }
            for name in sorted(self._workflows)
            if self._workflow_visible(self._workflows[name], repo)
            and not self._workflows[name].hidden
        ]

    def _workflow_visible(self, workflow: Workflow, repo: Repo) -> bool:
        if workflow.name in repo.disabled_workflows:
            return False
        if workflow.opt_in:
            return workflow.name in repo.enabled_workflows
        return True

    async def workflow_image_layer(self, name: str) -> str:
        """The workflow's Docker image layer (ADR 0005) — the Dockerfile fragment the runner
        composes onto the base image (e.g. github-peer-reviewed's `gh`). Empty when the workflow needs none."""
        return self._workflow(name).image_layer()

    async def workflow_execution(self, name: str) -> dict[str, Any]:
        """How the session service runs this workflow's tasks — everything the runner needs to route
        and launch, in one call so it fetches once and caches:

        * ``runner_type`` — ``"docker"`` (a task container) or ``"shell"`` (a host shell script);
        * ``script`` — the shell script a ``"shell"`` workflow runs (empty for ``"docker"``);
        * ``clone_repo`` — whether to clone the repo into the task dir (``"docker"`` always does);
        * ``workdir`` — a ``"shell"`` workflow's start-directory override (``None`` = the task dir).
        """
        workflow = self._workflow(name)
        return {
            "runner_type": workflow.runner_type,
            "script": workflow.shell_script(),
            "clone_repo": workflow.clone_repo,
            "workdir": workflow.shell_workdir,
        }

    def _workflow(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError:
            raise UnknownWorkflow(f"unknown workflow {name!r}") from None

    # -- tasks --------------------------------------------------------------------

    async def _save_task(self, task: Task) -> None:
        """Stamp ``updated_at`` and persist. All task mutations route through here."""
        task.updated_at = self._clock()
        await self._store.save_task(task)

    async def _save_container_state(self, task: Task) -> None:
        """Persist container-ownership fields (``claimed_by``) without stamping ``updated_at``.

        Container status changes (claim / release / reclaim) are runner bookkeeping, not task
        content mutations — they must not reorder the dashboard or wake watchers that key on
        meaningful task progress.
        """
        await self._store.save_task(task)

    async def create_task(
        self,
        repo_id: str,
        workflow_name: str,
        *,
        memo: str | None = None,
        governor_task_id: str | None = None,
        initial_prompt: str | None = None,
        artifacts: dict[str, str] | None = None,
        artifacts_b64: dict[str, str] | None = None,
        depends_on_task_ids: list[str] | None = None,
        sort_weight: int = 0,
    ) -> Task:
        repo = await self.get_repo(repo_id)  # ensure exists (raises NotFound)
        if governor_task_id is not None:
            await self.get_task(governor_task_id)  # ensure governor exists (raises NotFound)
        wf = self._workflow(workflow_name)
        if not self._workflow_visible(wf, repo):
            raise NotAuthorized(f"workflow {workflow_name!r} is not enabled for repo {repo_id!r}")
        now = self._clock()
        task = wf.start_task(
            self._id(), repo_id, at=now, memo=memo, initial_prompt=initial_prompt, repo=repo
        )
        task.governor_task_id = governor_task_id
        task.sort_weight = sort_weight
        task.created_at = now
        task.updated_at = now  # creation time = first mutation
        await self._store.create_task(task)
        _log.info("task %s: created (workflow=%s, repo=%s)", task.id, workflow_name, repo_id)
        for name, content in (artifacts or {}).items():
            await self.put_artifact(task.id, name, content.encode())
        for name, encoded in (artifacts_b64 or {}).items():  # binary artifacts arrive base64
            await self.put_artifact(task.id, name, decode_b64_artifact(name, encoded))
        if depends_on_task_ids:
            task = await self.set_dependencies(task.id, depends_on_task_ids)
        return task

    async def _require_orchestrator(self, actor_task_id: str) -> Task:
        """Authorize an orchestration action by ``actor_task_id``: the acting task must exist and
        its workflow must opt in (``Workflow.orchestrates``). Returns the acting task on success.

        The capability lives on the workflow (declarative, like ``skills``/``tools``), so the
        service stays workflow-name-agnostic — any workflow that sets ``orchestrates = True`` can
        create/seed other tasks.
        """
        actor = await self.get_task(actor_task_id)  # raises NotFound
        if not self._workflow(actor.workflow).orchestrates:
            raise NotAuthorized(
                f"task {actor_task_id!r} (workflow {actor.workflow!r}) may not orchestrate other tasks"
            )
        return actor

    async def create_task_as(
        self,
        actor_task_id: str,
        workflow_name: str,
        *,
        memo: str | None = None,
        initial_prompt: str | None = None,
        artifacts: dict[str, str] | None = None,
        artifacts_b64: dict[str, str] | None = None,
        depends_on_task_ids: list[str] | None = None,
        sort_weight: int = 0,
    ) -> Task:
        """Create a task **on behalf of an orchestrator task** — gated to orchestration workflows.

        The acting task (``actor_task_id``) must be one whose workflow ``orchestrates``; otherwise
        :class:`NotAuthorized`. The new task is created **in the orchestrator's own repo** — this
        first iteration deliberately can't create tasks in another repo, so there is no repo
        parameter to misuse. This is the create path the orchestration MCP tools use; the plain
        :meth:`create_task` (and REST ``POST /tasks``) remain the ungated user/dashboard path.
        """
        actor = await self._require_orchestrator(actor_task_id)
        return await self.create_task(
            actor.repo_id,
            workflow_name,
            memo=memo,
            governor_task_id=actor_task_id,
            initial_prompt=initial_prompt,
            artifacts=artifacts,
            artifacts_b64=artifacts_b64,
            depends_on_task_ids=depends_on_task_ids,
            sort_weight=sort_weight,
        )

    async def workflow_names_as(self, actor_task_id: str) -> list[str]:
        """List workflow names for an orchestrator task (gated): discovery for a child's ``workflow``."""
        await self._require_orchestrator(actor_task_id)
        return await self.workflow_names()

    async def get_task(self, task_id: str) -> Task:
        task = await self._store.get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r} does not exist")
        return task

    async def list_tasks(self) -> list[Task]:
        return await self._store.list_tasks()

    async def list_tasks_summary(self, *, terminal: bool | None = None) -> list[Task]:
        """Return tasks without history. Optionally filter to terminal-only or active-only."""
        tasks = await self._store.list_tasks_summary()
        if terminal is None:
            return tasks
        return [t for t in tasks if (t.state in TERMINAL_LABELS) == terminal]

    async def _tasks_snapshot(self, *, terminal: bool | None = None) -> tuple[int, list[Task]]:
        """Read the version before the query so the reported version is a lower bound.

        If a mutation commits during the ``await``, the version we already captured is from before
        it, so the client's next long-poll (``since=version``) unblocks immediately rather than
        waiting for ``MAX_WAIT_SECONDS``.
        """
        version = self.tasks_version()
        tasks = await self.list_tasks_summary(terminal=terminal)
        return version, tasks

    def tasks_version(self) -> int:
        """The change-feed version — bumped on every task mutation (ADR 0006 single writer) **and**
        on every ephemeral liveness change (registration, runner liveness, lifecycle phase), so a
        container coming up or a spawn phase advancing wakes a parked :meth:`subscribe_to_changes`
        long-poll just like a stored mutation does. The sum of both counters is monotonic."""
        return self._store.version() + self._ephemeral_epoch

    def subscribe_to_changes(self, listener: Callable[[], None]) -> None:
        """Register a callback fired (synchronously) after every change — stored *or* ephemeral.
        The HTTP layer wires an async wake-up here so ``GET /tasks`` can long-poll for changes."""
        self._store.subscribe(listener)
        self._change_listeners.append(listener)

    def _notify_change(self) -> None:
        """Record an ephemeral change (bump the epoch) and wake every subscribed listener — the
        ephemeral counterpart of the store bumping its version on a task mutation."""
        self._ephemeral_epoch += 1
        for listener in self._change_listeners:
            listener()

    async def legal_transitions(self, task_id: str) -> list[str]:
        """The states the task may move to next (its workflow's edges out of the current state)."""
        task = await self.get_task(task_id)
        return sorted(self._workflow(task.workflow).transitions(task.state))

    async def workflow_states(self, task_id: str) -> list[str]:
        """Every state of the task's workflow — the candidates for a free state-set (set_state)."""
        task = await self.get_task(task_id)
        return list(self._workflow(task.workflow).labels())

    async def operations(self, task_id: str) -> dict[str, str]:
        """The named core operations available now (verb → target state) — advance/drop."""
        task = await self.get_task(task_id)
        return self._workflow(task.workflow).operations(task.state)

    async def skills(self, task_id: str) -> list[Skill]:
        """The in-container skills for a task: the agnostic `provision` skill (every task names
        itself to get a branch, ADR 0011), the active workflow's own skills, and — for a repo
        opted into the review-artifact gate — tarot's **own** packaged authoring skill.

        Serving tarot's text rather than paraphrasing it keeps one copy of a contract tarot owns:
        the file formats change with tarot's validators, and the judgment it teaches (what to
        retitle, what a description is for) is exactly what a schema summary would lose."""
        task = await self.get_task(task_id)
        skills = [PROVISION_SKILL, *self._workflow(task.workflow).skills()]
        repo = await self.get_repo(task.repo_id)
        if self._tarot_gate.opted_in(repo):
            tarot_skill = authoring_skill(self._tarot_gate.cli)
            if tarot_skill is not None:
                skills.append(tarot_skill)
        return skills

    # -- tarot authoring passthroughs ---------------------------------------------
    #
    # Tarot's authoring skill has seven steps; four invoke the CLI. These are those four, run
    # host-side against the task's clone — the same directory the container sees at /workspace, so
    # a seed the agent asked for is computed from exactly the code it just wrote. The agent stays
    # the author: `strand_seed` and `check` write nothing at all, and `tour_scaffold` writes only
    # the stub file the agent then fills in.

    async def _tarot_target(self, task_id: str) -> tuple[Task, Repo, str, list[str]]:
        """The ``(task, repo, clone, base_args)`` for a tarot authoring tool, or refuse.

        Refuses — with the operator/agent-facing message, never a traceback — when the repo hasn't
        opted in, tarot isn't on this host, or the clone isn't readable here.
        """
        task = await self.get_task(task_id)
        repo = await self.get_repo(task.repo_id)
        if not self._tarot_gate.opted_in(repo):
            raise TarotGateRefused(
                f"repo {repo.id!r} hasn't opted into the tarot review gate "
                "(`capabilities.tarot_review`), so the tarot authoring tools aren't available here."
            )
        host = self.runner_host(task.claimed_by) if task.claimed_by else None
        unusable = self._tarot_gate.unusable_reason(task, runner_host=host)
        if unusable is not None:
            raise TarotGateRefused(unusable)
        assert task.clone is not None  # guaranteed by unusable_reason
        cli = self._tarot_gate.cli
        return task, repo, task.clone, cli.resolve_base_args(task.clone, repo.default_base)

    async def tarot_strand_seed(self, task_id: str) -> str:
        """`tarot strands suggest --json`: the detector-built strand seed, for the agent to edit.

        **Read-only** — ``--json`` prints the seed instead of writing ``.tarot/strands.json``, so
        nothing in the agent's working tree changes. What it supplies is the one thing an agent
        can't derive from a diff: tarot's own enumeration of changed nodes, which is the set
        ``strands check`` will insist is claimed exactly once.
        """
        _task, _repo, clone, base_args = await self._tarot_target(task_id)
        result = self._tarot_gate.cli.suggest(clone, base_args=base_args)
        if not result.ok:
            raise TarotGateRefused(result.output.strip() or "`tarot strands suggest` failed")
        return result.output

    async def tarot_check(self, task_id: str) -> str:
        """`tarot strands check` + `tarot tour check`, **without attempting a transition**.

        The tight authoring loop. Without it the only way for an agent to see its violations is to
        attempt an `advance` and be refused, which turns the gate from a backstop into the
        iteration mechanism and spends a transition per round.
        """
        task, _repo, clone, base_args = await self._tarot_target(task_id)
        outcome = self._tarot_gate.cli.check(clone, base_args=base_args)
        if outcome.missing_binary:
            raise TarotGateRefused(
                self._tarot_gate.unusable_reason(task) or "`tarot` is not available on this host"
            )
        if outcome.ok:
            return "tarot: the strand seed and every tour are valid."
        return outcome.output

    async def tarot_tour_scaffold(self, task_id: str, *, title: str = "PR walkthrough") -> str:
        """`tarot tour scaffold --from-strands`: step stubs built from the *edited* strand seed.

        The one authoring tool that **writes** (`.tarot/tours/<id>.json`; tarot has no ``--json``
        for scaffold). Worth it: the scaffold carries one chapter per strand with the author's own
        titles, a real trail/cursor per step, and blast-radius steps taken from tarot's call graph
        — none of which a hand enumeration produces — and leaves every note a ``TODO`` for the
        agent to replace with the narrative, which is the part that wants judgment.
        """
        _task, _repo, clone, base_args = await self._tarot_target(task_id)
        result = self._tarot_gate.cli.scaffold(clone, base_args=base_args, title=title)
        if not result.ok:
            raise TarotGateRefused(result.output.strip() or "`tarot tour scaffold` failed")
        return result.output.strip() or "tarot: wrote the tour scaffold."

    async def briefing(self, task_id: str) -> str:
        """A short briefing on the task's current phase (state + responsibilities + how it advances),
        rendered from the workflow so the in-container agent knows *where it is* (the hook emits it)."""
        task = await self.get_task(task_id)
        return await self._workflow(task.workflow).briefing(task, artifacts=self._artifacts)

    async def workflow_overview(self, task_id: str) -> str:
        """A one-time map of the task's whole workflow (the agent gets this in its system prompt)."""
        task = await self.get_task(task_id)
        return self._workflow(task.workflow).overview()

    async def apply_operation(
        self, task_id: str, operation: str, *, note: str | None = None
    ) -> Task:
        """Apply a named core operation (advance/drop) — a gated move along the declared graph."""
        task = await self.get_task(task_id)
        to_state = self._workflow(task.workflow).resolve_operation(task.state, operation)
        return await self.request_transition(task_id, to_state, trigger=operation, note=note)

    async def request_transition(
        self,
        task_id: str,
        to_state: str,
        *,
        trigger: str | None = None,
        note: str | None = None,
    ) -> Task:
        task = await self.get_task(task_id)
        wf = self._workflow(task.workflow)
        return await self._commit_transition(
            task, wf, to_state, force=False, trigger=trigger, note=note
        )

    async def _run_tarot_gate(self, task: Task, repo: Repo) -> None:
        """Verify the task's `.tarot/` review artifacts, refusing the transition if they fail.

        Runs for an `advance` out of ITERATING on an opted-in repo (:meth:`TarotGate.applies`).
        The outcome is recorded on the ``tarot-review-artifacts`` responsibility either way; on
        failure the recorded ``FAILED`` comment is persisted and :class:`TarotGateRefused` is
        raised **before** the transition, so the task stays where it is. The refusal — not the
        comment — is the enforcement (a ``FAILED``-with-comment promise counts as resolved), which
        is why this runs on every attempt rather than only while the promise is pending.
        """
        host = self.runner_host(task.claimed_by) if task.claimed_by else None
        decision = self._tarot_gate.evaluate(task, repo, runner_host=host)
        if decision.resolution is not None:
            self._record_tarot_resolution(task, *decision.resolution)
        if decision.allowed:
            return
        await self._save_task(task)  # persist the FAILED comment; the transition does not happen
        _log.info("task %s: tarot review gate refused advance", task.id)
        raise TarotGateRefused(decision.refusal or "the tarot review checks failed")

    @staticmethod
    def _record_tarot_resolution(task: Task, status: Status, comment: str) -> None:
        """Resolve the gate's responsibility, if the current state actually promised it.

        A repo can be opted in *after* a task entered ITERATING, in which case the promise was
        never seeded on this history entry and ``resolve_responsibility`` would raise. The gate
        still runs (and still refuses) — it just has nowhere to write the comment.
        """
        promised = {r.key for r in task.current_entry.responsibilities}
        if TAROT_RESPONSIBILITY_KEY in promised:
            task.resolve_responsibility(
                key=TAROT_RESPONSIBILITY_KEY, status=status, comment=comment
            )

    async def set_state(self, task_id: str, to_state: str, *, note: str | None = None) -> Task:
        """The user's free override: move the task to any state, bypassing the graph and the gate."""
        task = await self.get_task(task_id)
        wf = self._workflow(task.workflow)
        return await self._commit_transition(
            task, wf, to_state, force=True, trigger="set-state", note=note
        )

    async def _commit_transition(
        self,
        task: Task,
        wf: Workflow,
        to_state: str,
        *,
        force: bool,
        trigger: str | None,
        note: str | None,
    ) -> Task:
        from_state = task.state
        _log.info("task %s: %s → %s (trigger=%s)", task.id, from_state, to_state, trigger)
        repo = await self.get_repo(task.repo_id)
        if not force and self._tarot_gate.applies(
            task,
            repo,
            trigger=trigger,
            declared={r.key for r in wf.responsibilities(task.state, repo=repo)},
        ):
            await self._run_tarot_gate(task, repo)
        if force:
            wf.force_transition(
                task, to_state, at=self._clock(), trigger=trigger, note=note, repo=repo
            )
        else:
            wf.apply_transition(
                task, to_state, at=self._clock(), trigger=trigger, note=note, repo=repo
            )
        # Deterministic lifecycle hook (e.g. seed the plan on plan acceptance) — may touch the
        # task/artifacts; run before the single save so any task mutation persists with it.
        await wf.on_transition(
            task, from_state=from_state, to_state=task.state, artifacts=self._artifacts
        )
        await self._save_task(task)
        if to_state == Dropped.label:
            await self._cascade_drop_governed(task.id, trigger=trigger, note=note)
        return task

    async def _cascade_drop_governed(
        self, governor_id: str, *, trigger: str | None, note: str | None
    ) -> None:
        """Drop every non-terminal task governed by governor_id.

        Called after a governor lands in DROPPED. Each child's own _commit_transition also
        runs this, so nested governor chains cascade without an explicit outer loop."""
        count = 0
        for child in await self._store.list_tasks_summary():
            if child.governor_task_id == governor_id and child.state not in TERMINAL_LABELS:
                await self.request_transition(
                    child.id, Dropped.label, trigger="cascade-drop", note=note
                )
                count += 1
        if count:
            _log.info("task %s: cascade-dropped %d governed task(s)", governor_id, count)

    async def resolve_responsibility(
        self, task_id: str, key: str, *, status: Status, comment: str | None = None
    ) -> Task:
        """Record the agent's progress on one promised responsibility (fulfilled in place)."""
        task = await self.get_task(task_id)
        task.resolve_responsibility(key=key, status=status, comment=comment)
        await self._save_task(task)
        _log.debug("task %s: responsibility %s → %s", task_id, key, status)
        return task

    async def set_slug(self, task_id: str, slug: str) -> Task:
        task = await self.get_task(task_id)
        previous = task.slug
        task.slug = slug
        await self._save_task(task)
        _log.info("task %s: slug → %s", task_id, slug)
        # Expose the task's artifacts under the slug alias; drop a stale one on a re-slug so the
        # tasks/ dir keeps a single live alias per task (the symlinks live on the artifact store).
        if previous is not None and previous != slug:
            await self._artifacts.unlink_slug(previous)
        await self._artifacts.link_slug(task_id, slug)
        return task

    async def set_url(self, task_id: str, url: str) -> Task:
        """Record an external URL for the task (its PR, an issue, …); the dashboard's `p`
        hotkey opens it. A plain recorded fact, like the slug — no transition, no git."""
        task = await self.get_task(task_id)
        task.url = url
        await self._save_task(task)
        _log.debug("task %s: url → %s", task_id, url)
        return task

    async def set_tokens_used(self, task_id: str, tokens_used: int) -> Task:
        """Record the cumulative tokens the container's claude has used (its Stop hook reports the
        recomputed session total). A plain recorded fact, like the slug — no transition, no git."""
        task = await self.get_task(task_id)
        task.tokens_used = tokens_used
        await self._save_task(task)
        return task

    async def set_token_estimate(self, task_id: str, token_estimate: int) -> Task:
        """Record the agent's forecast of the total tokens this task will consume (set once during
        planning). A plain recorded fact, like the slug — no transition, no git."""
        task = await self.get_task(task_id)
        task.token_estimate = token_estimate
        await self._save_task(task)
        return task

    async def set_turn(self, task_id: str, turn: Actor) -> Task:
        """Flip who holds the turn within a state (the in-container hooks' callback).

        This is the agnostic agent↔user ball tracking (ADR 0004). It leaves ``blocked``
        untouched, so a deliberate block survives turn flips.
        """
        task = await self.get_task(task_id)
        task.turn = turn
        await self._save_task(task)
        return task

    async def set_blocked(self, task_id: str, blocked: bool) -> Task:
        """Set/clear the task's deliberate ``blocked`` marker (orthogonal to the turn)."""
        task = await self.get_task(task_id)
        task.blocked = blocked
        await self._save_task(task)
        _log.debug("task %s: blocked=%s", task_id, blocked)
        return task

    async def set_snooze(self, task_id: str, until: str | None) -> Task:
        """Record or clear an operator snooze deadline without interpreting the clock.

        The value is stored verbatim (any ISO-8601 string, or ``None`` to clear); whether a finite
        deadline is active is decided by the dashboard alone. Leaves ``state``/``turn``/``blocked``
        untouched — a plain recorded fact, like the url.
        """
        task = await self.get_task(task_id)
        task.snoozed_until = until
        await self._save_task(task)
        _log.debug("task %s: snoozed_until → %s", task_id, until)
        return task

    async def set_sort_weight(self, task_id: str, sort_weight: int) -> Task:
        """Set the task's dashboard sort weight (default 0; higher sorts first).

        A plain recorded fact, like the url: ranks above the ``updated_at`` timestamp but below
        state/turn in the dashboard ordering. Leaves ``state``/``turn``/``blocked`` untouched.
        """
        task = await self.get_task(task_id)
        task.sort_weight = sort_weight
        await self._save_task(task)
        _log.debug("task %s: sort_weight → %s", task_id, sort_weight)
        return task

    async def set_governor(self, task_id: str, governor_task_id: str | None) -> Task:
        """Set or clear the governor task for ``task_id``.

        Pass a non-None ``governor_task_id`` to link an overseer; pass ``None`` to remove it.
        When non-None, the governor task must exist (raises :class:`NotFound` if not).
        """
        task = await self.get_task(task_id)
        if governor_task_id is not None:
            await self.get_task(governor_task_id)  # ensure governor exists
        task.governor_task_id = governor_task_id
        await self._save_task(task)
        return task

    async def set_dependencies(self, task_id: str, dep_ids: list[str]) -> Task:
        """Replace the task's dependency list with ``dep_ids``.

        Each ID must reference an existing task; self-references are rejected. Passing an
        empty list clears all dependencies. This is a plain recorded fact — the state machine
        does not enforce the constraint.
        """
        if task_id in dep_ids:
            raise ValueError(f"task {task_id!r} cannot depend on itself")
        task = await self.get_task(task_id)
        for dep_id in dep_ids:
            if await self._store.get_task(dep_id) is None:
                raise NotFound(f"dependency task {dep_id!r} does not exist")
        task.depends_on_task_ids = list(dep_ids)
        await self._save_task(task)
        return task

    # -- claim (a runner owns the task; the spawn gate, ADR 0008) --------------------------

    async def claim(self, task_id: str, runner_id: str) -> Task:
        """Claim an unclaimed task for ``runner_id`` (a session service claims before spawning).

        Compare-and-set: succeeds if the task is unclaimed (idempotent if this runner already holds
        it); raises :class:`AlreadyClaimed` if a different runner does. The store is the single
        writer, so the check-and-set is serialized.
        """
        task = await self.get_task(task_id)
        if task.claimed_by not in (None, runner_id):
            raise AlreadyClaimed(f"task {task_id!r} is already claimed by {task.claimed_by!r}")
        task.claimed_by = runner_id
        self.clear_lifecycle(
            task_id
        )  # drop any stale phase from a prior owner; this spawn re-reports
        await self._save_container_state(task)
        _log.info("task %s: claimed by runner %s", task_id, runner_id)
        return task

    async def release(self, task_id: str) -> Task:
        """Release a task's claim (back to unclaimed) so it can be re-claimed / respawned. Clears any
        reported lifecycle phase so the task reads ``queued`` until the runner re-claims + re-reports."""
        task = await self.get_task(task_id)
        task.claimed_by = None
        self.clear_lifecycle(task_id)
        await self._save_container_state(task)
        _log.info("task %s: claim released", task_id)
        return task

    # -- provisioning (the session service does the host git; the service only records) ---

    async def record_provisioning(self, task_id: str, *, branch: str, clone: str) -> Task:
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
        task = await self.get_task(task_id)
        if task.slug is None:
            raise ValueError("cannot record provisioning before the task's slug is set")
        task.branch = branch
        task.clone = clone
        await self._save_task(task)
        _log.info("task %s: provisioned (branch=%s)", task_id, branch)
        return task

    # -- asks (ask-the-author: a reviewer interrogates a task's agent) ---------------------
    #
    # Ephemeral like a registration/lifecycle: review-time questions delivered to the task's claude
    # session and their answers, held in memory (:attr:`_asks`) — never a workflow transition, so an
    # ask neither changes state nor seeds responsibilities. A reviewer may **queue** several questions
    # per task (a bounded FIFO of PENDING asks); the **session service** delivers them **strictly one
    # at a time** — the next only once the previous is ANSWERED or GONE — because answer extraction
    # anchors on the transcript's turn boundaries, so a second question injected mid-answer would
    # truncate the first reply. The task service tracks the queue and enforces its bound; the
    # container's Stop hook records each answer.

    _UNANSWERED = frozenset({AskStatus.PENDING, AskStatus.DELIVERED})

    def _task_asks(self, task_id: str) -> list[Ask]:
        """This task's asks, oldest first (created_at is an ISO string, so lexical == chronological)."""
        asks = [a for a in self._asks.values() if a.task_id == task_id]
        return sorted(asks, key=lambda a: a.created_at or "")

    def _ask_queue(self, task_id: str) -> list[Ask]:
        """The task's live ask queue: unanswered asks (PENDING/DELIVERED), oldest (head) first.
        Answered/gone asks have left the queue. This is what ``answering N of M`` counts over."""
        return [a for a in self._task_asks(task_id) if a.status in self._UNANSWERED]

    def _get_ask(self, task_id: str, ask_id: str) -> Ask:
        ask = self._asks.get(ask_id)
        if ask is None or ask.task_id != task_id:
            raise NotFound(f"ask {ask_id!r} does not exist for task {task_id!r}")
        return ask

    async def create_ask(self, task_id: str, question: str, context: str = "") -> Ask:
        """Append a question to the task's ask queue (the session service delivers it in turn).

        Always accepts while the queue has room — a reviewer can stack several questions and read the
        answers as they land. Only a **full** queue (:data:`ASK_QUEUE_CAP` unanswered asks) is
        rejected, with :class:`AskQueueFull`. Wakes the change feed so the host daemon's ask worker
        picks up the head of the queue.
        """
        await self.get_task(task_id)  # ensure the task exists (raises NotFound)
        if len(self._ask_queue(task_id)) >= ASK_QUEUE_CAP:
            raise AskQueueFull(
                f"task {task_id!r} ask queue is full ({ASK_QUEUE_CAP}); wait for answers before asking more"
            )
        ask = Ask(
            id=self._id(),
            task_id=task_id,
            question=question,
            context=context,
            created_at=self._clock(),
        )
        self._asks[ask.id] = ask
        self._notify_change()  # wake the host daemon's ask worker (it reads pending_ask_id)
        _log.info(
            "task %s: ask %s queued (position %d)", task_id, ask.id, len(self._ask_queue(task_id))
        )
        return ask

    def get_ask(self, task_id: str, ask_id: str) -> Ask:
        """The ask (raises :class:`NotFound` if unknown; :class:`AskGone` if its volume was reaped)."""
        ask = self._get_ask(task_id, ask_id)
        if ask.status is AskStatus.GONE:
            raise AskGone(
                f"ask {ask_id!r}: the task's container/volume is gone; the agent can't be resumed"
            )
        return ask

    def ask_position(self, task_id: str, ask_id: str) -> tuple[int, int]:
        """``(position, queue_length)`` for an ask: its 1-based place in the live queue (the head,
        being delivered/answered now, is 1) and the queue's length. Position ``0`` means the ask has
        left the queue (answered or gone). Lets the review tool show ``answering 1 of 3``."""
        queue = self._ask_queue(task_id)
        ids = [a.id for a in queue]
        position = ids.index(ask_id) + 1 if ask_id in ids else 0
        return position, len(queue)

    def outstanding_ask(self, task_id: str) -> Ask | None:
        """The task's currently **delivered** (in-flight, unanswered) ask, or ``None`` — what the
        container Stop hook checks to attribute a reply. Delivery is serialized, so there is at most
        one; the rest of the queue is still PENDING behind it, invisible to attribution."""
        for ask in self._task_asks(task_id):
            if ask.status is AskStatus.DELIVERED:
                return ask
        return None

    def pending_ask_id(self, task_id: str) -> str | None:
        """The id of the task's next **deliverable** ask (the head of the queue), or ``None``.

        Overlaid on the task's serialized form so the host daemon's ask worker spots a deliverable ask
        without a per-task request. Enforces serialization at the source: while an ask is in flight
        (DELIVERED, awaiting its answer) this returns ``None``, so the worker holds the rest of the
        queue; it clears to the next PENDING head only once the in-flight one is ANSWERED or GONE.
        """
        queue = self._ask_queue(task_id)
        if any(a.status is AskStatus.DELIVERED for a in queue):
            return None  # one delivered-unanswered at a time — hold the queue until it resolves
        head = queue[0] if queue else None
        return head.id if head is not None and head.status is AskStatus.PENDING else None

    def mark_ask_delivered(self, task_id: str, ask_id: str) -> Ask:
        """Mark an ask delivered (the session service handed it to the agent); wakes the feed."""
        ask = self._get_ask(task_id, ask_id)
        ask.status = AskStatus.DELIVERED
        self._notify_change()
        _log.info("task %s: ask %s delivered", task_id, ask_id)
        return ask

    def mark_ask_gone(self, task_id: str, ask_id: str) -> Ask:
        """Mark ``ask_id`` — and every other unanswered ask queued behind it — GONE.

        The config volume being reaped means the agent's session is unrecoverable, which is true for
        the whole queue, not just the head: draining it lets the review tool offer a surrogate
        **once** rather than rediscovering the dead session per question. Returns the named ask.
        """
        named = self._get_ask(task_id, ask_id)
        drained = 0
        for ask in self._ask_queue(task_id):
            ask.status = AskStatus.GONE
            drained += 1
        self._notify_change()
        _log.info(
            "task %s: ask %s gone (volume reaped); drained %d queued ask(s)",
            task_id,
            ask_id,
            drained,
        )
        return named

    def record_ask_answer(self, task_id: str, ask_id: str, answer: str) -> Ask:
        """Record the agent's reply (the container Stop hook extracts it from the transcript). The
        ask leaves the queue (ANSWERED), so the worker's next pass delivers the queue's new head."""
        ask = self._get_ask(task_id, ask_id)
        ask.answer = answer
        ask.status = AskStatus.ANSWERED
        self._notify_change()
        _log.info("task %s: ask %s answered", task_id, ask_id)
        return ask

    async def lookup_task(
        self, *, repo_id: str | None = None, branch: str | None = None, url: str | None = None
    ) -> Task:
        """Find the task matching a branch (with its repo) or a URL — the review tool's entry point.

        Exactly one selector is expected: ``repo_id`` + ``branch``, or ``url``. Raises
        :class:`ValueError` for a malformed request and :class:`NotFound` if nothing matches. Returns
        the full task (history included), so the review tool gets the same shape as ``GET /tasks/{id}``.
        """
        if url is not None:
            if repo_id is not None or branch is not None:
                raise ValueError("pass either url, or repo_id + branch — not both")
            found = await self._store.find_task_by_url(url)
            if found is None:
                raise NotFound(f"no task with url {url!r}")
        elif repo_id is not None and branch is not None:
            found = await self._store.find_task_by_branch(repo_id, branch)
            if found is None:
                raise NotFound(f"no task on repo {repo_id!r} with branch {branch!r}")
        else:
            raise ValueError("pass either url, or repo_id + branch")
        return await self.get_task(
            found.id
        )  # re-read for full history (the lookup is history-less)

    # -- artifacts ----------------------------------------------------------------

    async def put_artifact(self, task_id: str, name: str, content: bytes) -> None:
        await self.get_task(task_id)  # ensure the task exists
        await self._artifacts.put(task_id, name, content)
        _log.debug("task %s: artifact %s written", task_id, name)

    async def get_artifact(self, task_id: str, name: str) -> bytes | None:
        await self.get_task(task_id)
        return await self._artifacts.get(task_id, name)

    async def list_artifacts(self, task_id: str) -> list[str]:
        await self.get_task(task_id)
        return await self._artifacts.list(task_id)

    # -- liveness -----------------------------------------------------------------
    #
    # Liveness is connection-scoped: a container holds the ``/live`` stream open for its whole
    # lifetime, the service registers on connect and removes on disconnect. Death (clean exit,
    # ``docker stop``, ``SIGKILL`` / ``docker rm --force``, crash) drops the connection and is
    # noticed immediately — no heartbeat to miss, no wall-clock TTL to age out (so a container
    # that dies can't linger as "live", and ``registrations`` reads no clock at all).

    async def register(
        self, task_id: str, container_id: str, runner_id: str | None = None
    ) -> Registration:
        await self.get_task(task_id)  # ensure the task exists
        reg = Registration(
            id=self._id(),
            task_id=task_id,
            container_id=container_id,
            runner_id=runner_id,
            registered_at=self._clock(),
        )
        self._registrations[reg.id] = reg
        self._notify_change()  # a container going live wakes the dashboard's long-poll
        _log.info("task %s: container registered (reg=%s)", task_id, reg.id)
        return reg

    async def deregister(self, registration_id: str) -> None:
        reg = self._registrations.pop(registration_id, None)
        if reg is not None:
            self._notify_change()  # a container dropping wakes the long-poll (live → down/awaiting)
            _log.info("task %s: registration %s released", reg.task_id, registration_id)

    def registrations(self, task_id: str | None = None) -> list[Registration]:
        return [r for r in self._registrations.values() if task_id is None or r.task_id == task_id]

    # -- container lifecycle (the session service reports its spawn progress) -------------
    #
    # The runner pushes a :class:`ContainerLifecycle` phase as it claims → prepares → builds →
    # starts a container, so the feedback that used to be invisible (a slow ``docker build``, a
    # container that never came up) surfaces on the dashboard. Ephemeral like a registration —
    # cleared on claim release/reclaim — and folded with registration presence + runner liveness
    # into the displayed :class:`ContainerStatus` by :meth:`container_status`.

    async def report_lifecycle(
        self, task_id: str, runner_id: str, phase: LifecyclePhase, detail: str | None = None
    ) -> ContainerLifecycle:
        """Record the runner's latest spawn phase for a task (an upsert; the newest wins)."""
        await self.get_task(task_id)  # ensure the task exists
        lifecycle = ContainerLifecycle(
            task_id=task_id, runner_id=runner_id, phase=phase, detail=detail, at=self._clock()
        )
        self._lifecycles[task_id] = lifecycle
        self._notify_change()
        _log.debug("task %s: lifecycle phase=%s", task_id, phase.value)
        return lifecycle

    def clear_lifecycle(self, task_id: str) -> None:
        """Drop a task's reported phase (idempotent — only wakes the feed if one was present)."""
        if self._lifecycles.pop(task_id, None) is not None:
            self._notify_change()
            _log.debug("task %s: lifecycle cleared", task_id)

    def lifecycle(self, task_id: str) -> ContainerLifecycle | None:
        """The task's latest reported spawn phase, or ``None`` if none is current."""
        return self._lifecycles.get(task_id)

    def container_status(self, task: Task) -> ContainerStatus:
        """The task's composed container-lifecycle status (the single string the dashboard shows):
        fold the reported phase together with registration presence + runner liveness."""
        lifecycle = self._lifecycles.get(task.id)
        return compose_container_status(
            terminal=task.state in TERMINAL_LABELS,
            claimed=task.claimed_by is not None,
            registered=bool(self.registrations(task.id)),
            runner_live=task.claimed_by in self.live_runners(),
            phase=lifecycle.phase if lifecycle is not None else None,
        )

    # -- host (runner) liveness + reclaim ------------------------------------------
    #
    # The same connection-drop liveness as containers, one layer up: a runner (session service)
    # holds the ``/runners/{id}/live`` stream open for its whole life, so the control plane knows
    # which hosts are alive without a heartbeat or a wall-clock TTL. This is what makes **reclaim**
    # possible — a claim (``claimed_by``) used to linger forever when its runner died, with no way
    # to tell "runner dead" from "runner idle"; now a dead runner falls out of ``live_runners`` and
    # an operator (or a future supervisor) can release its claims so a healthy host respawns them.

    async def register_runner(
        self, runner_id: str, *, host: str | None = None
    ) -> RunnerRegistration:
        reg = RunnerRegistration(
            id=self._id(), runner_id=runner_id, registered_at=self._clock(), host=host
        )
        self._runner_registrations[reg.id] = reg
        self._notify_change()  # a runner (re)connecting can flip its tasks disconnected → …
        _log.info("runner %s: registered (reg=%s, host=%s)", runner_id, reg.id, host)
        return reg

    async def deregister_runner(self, registration_id: str) -> None:
        reg = self._runner_registrations.pop(registration_id, None)
        if reg is not None:
            self._notify_change()  # a runner dropping flips its claimed tasks → disconnected
            _log.info("runner %s: deregistered", reg.runner_id)

    def live_runners(self) -> set[str]:
        """The set of runner ids currently holding a host-liveness connection (no clock read)."""
        return {r.runner_id for r in self._runner_registrations.values()}

    def runner_host(self, runner_id: str) -> str | None:
        """The hostname the runner registered with, or ``None`` if unknown / not registered."""
        for reg in self._runner_registrations.values():
            if reg.runner_id == runner_id:
                return reg.host
        return None

    def live_runner_registrations(self) -> list[RunnerRegistration]:
        """One registration per distinct live runner id (deduplicated; stable order for REST)."""
        seen: dict[str, RunnerRegistration] = {}
        for reg in self._runner_registrations.values():
            seen.setdefault(reg.runner_id, reg)
        return sorted(seen.values(), key=lambda r: r.runner_id)

    async def reclaim(self, runner_id: str) -> list[Task]:
        """Release every non-terminal task claimed by ``runner_id`` so a healthy host can re-claim
        and respawn it. The operator-gated answer to a dead runner (justification 2): its containers
        died with it, but its claims would otherwise linger forever.

        Connection-driven and **clock-free** — "dead" is the caller's judgement (the runner is
        absent from :meth:`live_runners`); reclaim only releases the claims, it adds no TTL. Skips
        terminal tasks (nothing to respawn) and is idempotent (a second call finds nothing to do).
        Auto-triggering this on disconnect is deliberately *not* done here: with the auto-claiming
        spawner it would respawn a duplicate container on a transient host blip, so the release stays
        a deliberate action until spawn-dedup exists."""
        reclaimed = []
        for task in await self._store.list_tasks():
            if task.claimed_by == runner_id and task.state not in TERMINAL_LABELS:
                task.claimed_by = None
                self.clear_lifecycle(task.id)  # the dead runner's phase is stale; start clean
                await self._save_container_state(task)
                reclaimed.append(task)
        if reclaimed:
            _log.info("runner %s: reclaim released %d task(s)", runner_id, len(reclaimed))
        return reclaimed
