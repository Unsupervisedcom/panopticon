"""The task service REST API (FastAPI).

The dashboard, the runner, and in-container skills are clients of this API. In-container
agents also reach task operations/artifacts over **MCP**: ``create_app`` mounts the MCP
streamable-HTTP app (see :mod:`panopticon.taskservice.mcp`) at ``/mcp``, so the same control
plane serves REST and MCP. ``create_app`` builds an app around an injected
:class:`~panopticon.taskservice.service.TaskService`, so tests can wire a deterministic one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from panopticon.core.artifacts import ArtifactError
from panopticon.core.models import Actor, Repo, Status
from panopticon.core.store import AlreadyExists, NotFound, StoreError
from panopticon.core.workflow import IllegalTransition, InvalidWorkflow, ResponsibilitiesNotMet
from panopticon.taskservice.service import AlreadyClaimed, TaskService, UnknownWorkflow

# -- wire schemas -------------------------------------------------------------------


# ``*Out`` models read straight off the domain objects (`model_validate`): their fields match
# the domain attribute names, so `from_attributes=True` does the conversion — incl. nested
# Task -> History -> Responsibility — with no hand-written copying.


class ResponsibilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    description: str
    status: Status
    comment: str | None = None


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: str
    from_state: str | None
    to_state: str
    trigger: str | None = None
    note: str | None = None
    responsibilities: list[ResponsibilityOut] = []


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    repo_id: str
    workflow: str
    state: str
    turn: Actor
    blocked: bool
    memo: str | None  # free-text intent collected at creation (shown in the summary)
    slug: str | None
    url: str | None  # an optional external URL (PR, issue, …); the dashboard's `p` hotkey opens it
    branch: str | None
    clone: str | None
    claimed_by: str | None  # the runner that owns this task (the spawn gate), or None
    tokens_used: int | None  # cumulative tokens the container's claude has used (None until reported)
    provisioned: bool  # computed (Task.provisioned): branch + clone recorded
    history: list[HistoryOut]


class RepoIn(BaseModel):
    id: str
    name: str
    git_url: str
    default_base: str = "main"
    env_file: str | None = None
    creds_volume: str | None = None
    image_layer: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    default_base: str
    env_file: str | None = None
    creds_volume: str | None = None
    image_layer: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RepoPatchIn(BaseModel):
    # All fields optional: a PATCH carries only what changes. ``model_dump(exclude_unset=True)``
    # then tells "field omitted" from "field explicitly set to null", so a partial update can't
    # null out a field the operator didn't touch. ``id`` is the key — present here only so a
    # mismatched body is rejected (the path id is authoritative).
    id: str | None = None
    name: str | None = None
    git_url: str | None = None
    default_base: str | None = None
    env_file: str | None = None
    creds_volume: str | None = None
    image_layer: str | None = None
    capabilities: dict[str, Any] | None = None


class CreateTaskIn(BaseModel):
    repo_id: str
    workflow: str
    memo: str | None = None


class ResponsibilityIn(BaseModel):
    key: str
    status: Status
    comment: str | None = None


class TransitionIn(BaseModel):
    to_state: str
    trigger: str | None = None
    note: str | None = None


class SlugIn(BaseModel):
    slug: str


class UrlIn(BaseModel):
    url: str


class TokensUsedIn(BaseModel):
    tokens_used: int


class StateIn(BaseModel):
    state: str


class ProvisioningIn(BaseModel):
    branch: str
    clone: str


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    instructions: str


class TurnIn(BaseModel):
    turn: Actor


class BlockedIn(BaseModel):
    blocked: bool


class ClaimIn(BaseModel):
    runner_id: str


class RegisterIn(BaseModel):
    container_id: str
    runner_id: str | None = None


class RegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    container_id: str
    runner_id: str | None
    registered_at: str
    last_seen: str


# -- block-until-change feed --------------------------------------------------------

#: The header carrying the store's change-feed version on every ``GET /tasks`` response — the
#: cursor a client echoes back as ``?since=`` to long-poll for the next change.
TASKS_VERSION_HEADER = "X-Tasks-Version"

#: Ceiling on a single long-poll's hold time (seconds). A client asking to wait longer just gets
#: a snapshot at the cap and re-requests — keeps a connection from parking indefinitely.
MAX_WAIT_SECONDS = 60.0


class ChangeFeed:
    """An async broadcast over the store's change counter — the HTTP side of block-until-change.

    The store bumps its synchronous :meth:`~panopticon.core.store.Store.version` on every task
    mutation and calls :meth:`notify` (a subscribed listener). :meth:`wait` parks a request on an
    :class:`asyncio.Event` until the next ``notify`` (or a timeout), then returns the current
    version. The asyncio lives here, not in ``core`` — the store stays clock-free and push-free.

    All mutations arrive over HTTP/MCP and run on the event loop, so ``notify`` (which sets the
    event) and the waiters share one thread; no locking is needed.
    """

    def __init__(self, version: Callable[[], int]) -> None:
        self._version = version
        self._changed = asyncio.Event()

    def notify(self) -> None:
        """Wake every parked waiter, then arm a fresh event for the next round (broadcast)."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, since: int, timeout: float) -> int:
        """Return the current version once it differs from ``since``, or after ``timeout`` seconds.

        Returns immediately when the version already moved (any difference — including a service
        restart that reset the counter — counts, so a stale cursor never blocks forever).
        """
        if self._version() != since:
            return self._version()
        changed = self._changed  # capture before awaiting: notify() swaps in a fresh event
        try:
            await asyncio.wait_for(changed.wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        return self._version()


def create_app(service: TaskService) -> FastAPI:
    # MCP over streamable HTTP, mounted at /mcp on the same control plane (operations=tools,
    # artifacts=resources). Its path is set to "/" so the mount point *is* the endpoint (/mcp).
    # The session manager must run for the app's lifetime, so its context is driven by the
    # parent FastAPI lifespan (a mounted sub-app's own lifespan isn't run by the parent).
    # Imported here, not at module scope: mcp.py imports our ``*Out`` schemas (would cycle).
    from panopticon.taskservice.mcp import build_mcp_server

    mcp = build_mcp_server(service)
    mcp.settings.streamable_http_path = "/"
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="panopticon task service", version="0.0.1", lifespan=lifespan)

    # The block-until-change feed: a store mutation bumps the version + wakes parked GET /tasks
    # long-polls (the seam the daemons/dashboard migrate onto, replacing their interval re-polls).
    feed = ChangeFeed(service.tasks_version)
    service.subscribe_to_changes(feed.notify)

    # -- error mapping: domain exceptions -> HTTP status --------------------------

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AlreadyExists)
    async def _conflict(_: Request, exc: AlreadyExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, exc: IllegalTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ResponsibilitiesNotMet)
    async def _responsibilities(_: Request, exc: ResponsibilitiesNotMet) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UnknownWorkflow)
    async def _unknown_wf(_: Request, exc: UnknownWorkflow) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(InvalidWorkflow)
    async def _invalid_wf(_: Request, exc: InvalidWorkflow) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ArtifactError)
    async def _artifact(_: Request, exc: ArtifactError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # -- health & discovery -------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workflows")
    async def list_workflows() -> list[str]:
        return service.workflow_names()

    @app.get("/workflows/{name}/image-layer")
    async def workflow_image_layer(name: str) -> dict[str, str]:
        """The workflow's Dockerfile layer (ADR 0005); the runner composes it onto the base."""
        return {"layer": service.workflow_image_layer(name)}

    # -- repos --------------------------------------------------------------------

    @app.post("/repos", status_code=201)
    async def create_repo(body: RepoIn) -> RepoOut:
        repo = service.create_repo(Repo(**body.model_dump()))
        return RepoOut.model_validate(repo)

    @app.get("/repos")
    async def list_repos() -> list[RepoOut]:
        return [RepoOut.model_validate(r) for r in service.list_repos()]

    @app.get("/repos/{repo_id}")
    async def get_repo(repo_id: str) -> RepoOut:
        return RepoOut.model_validate(service.get_repo(repo_id))

    @app.patch("/repos/{repo_id}")
    async def update_repo(repo_id: str, body: RepoPatchIn) -> RepoOut:
        # exclude_unset → only the fields the caller actually sent; the service merges them
        # onto the stored repo (untouched fields, e.g. image_layer/capabilities, are preserved).
        changes = body.model_dump(exclude_unset=True)
        try:
            repo = service.update_repo(repo_id, changes)
        except ValueError as exc:  # e.g. attempting to change the id
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RepoOut.model_validate(repo)

    # -- tasks --------------------------------------------------------------------

    @app.post("/tasks", status_code=201)
    async def create_task(body: CreateTaskIn) -> TaskOut:
        return TaskOut.model_validate(
            service.create_task(body.repo_id, body.workflow, memo=body.memo)
        )

    @app.get("/tasks")
    async def list_tasks(
        response: Response,
        wait: float | None = Query(
            default=None,
            ge=0,
            description="Block up to this many seconds for a change past ?since before returning "
            f"(capped at {MAX_WAIT_SECONDS:g}s). Omit for an immediate snapshot.",
        ),
        since: int = Query(
            default=0,
            description="The X-Tasks-Version a client last saw; with ?wait, return once the "
            "version differs from it (block-until-change).",
        ),
    ) -> list[TaskOut]:
        # Every response carries the current version in X-Tasks-Version so a client can echo it
        # back as ?since=. With ?wait the request parks until the version moves past ?since (or
        # the cap elapses); without it, it's an immediate snapshot — today's behaviour.
        if wait is not None:
            version = await feed.wait(since=since, timeout=min(wait, MAX_WAIT_SECONDS))
        else:
            version = service.tasks_version()
        # No await between reading the version and the snapshot, so they're consistent: a mutation
        # (which runs on this same loop) can't interleave to leave the body ahead of the header.
        tasks = [TaskOut.model_validate(t) for t in service.list_tasks()]
        response.headers[TASKS_VERSION_HEADER] = str(version)
        return tasks

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> TaskOut:
        return TaskOut.model_validate(service.get_task(task_id))

    @app.get("/tasks/{task_id}/transitions")
    async def list_transitions(task_id: str) -> list[str]:
        return service.legal_transitions(task_id)

    @app.get("/tasks/{task_id}/operations")
    async def list_operations(task_id: str) -> dict[str, str]:
        return service.operations(task_id)

    @app.post("/tasks/{task_id}/operations/{operation}")
    async def apply_operation(task_id: str, operation: str) -> TaskOut:
        return TaskOut.model_validate(service.apply_operation(task_id, operation))

    @app.get("/tasks/{task_id}/states")
    async def list_states(task_id: str) -> list[str]:
        return service.workflow_states(task_id)

    @app.get("/tasks/{task_id}/skills")
    async def list_skills(task_id: str) -> list[SkillOut]:
        return [SkillOut.model_validate(s) for s in service.skills(task_id)]

    @app.get("/tasks/{task_id}/briefing")
    async def get_briefing(task_id: str) -> dict[str, str]:
        """The agent's current-phase briefing (the container's user-prompt hook emits it)."""
        return {"briefing": service.briefing(task_id)}

    @app.get("/tasks/{task_id}/workflow-overview")
    async def get_workflow_overview(task_id: str) -> dict[str, str]:
        """The whole-workflow map (the agent launcher puts it in claude's system prompt)."""
        return {"overview": service.workflow_overview(task_id)}

    @app.put("/tasks/{task_id}/state")
    async def set_state(task_id: str, body: StateIn) -> TaskOut:
        return TaskOut.model_validate(service.set_state(task_id, body.state))

    @app.post("/tasks/{task_id}/transition")
    async def transition(task_id: str, body: TransitionIn) -> TaskOut:
        return TaskOut.model_validate(
            service.request_transition(
                task_id, body.to_state, trigger=body.trigger, note=body.note
            )
        )

    @app.post("/tasks/{task_id}/responsibilities")
    async def resolve_responsibility(task_id: str, body: ResponsibilityIn) -> TaskOut:
        try:
            task = service.resolve_responsibility(
                task_id, body.key, status=body.status, comment=body.comment
            )
        except ValueError as exc:  # unknown key / PENDING / FAILED without a comment
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TaskOut.model_validate(task)

    @app.put("/tasks/{task_id}/slug")
    async def set_slug(task_id: str, body: SlugIn) -> TaskOut:
        return TaskOut.model_validate(service.set_slug(task_id, body.slug))

    @app.put("/tasks/{task_id}/url")
    async def set_url(task_id: str, body: UrlIn) -> TaskOut:
        return TaskOut.model_validate(service.set_url(task_id, body.url))

    @app.put("/tasks/{task_id}/tokens-used")
    async def set_tokens_used(task_id: str, body: TokensUsedIn) -> TaskOut:
        return TaskOut.model_validate(service.set_tokens_used(task_id, body.tokens_used))

    @app.put("/tasks/{task_id}/turn")
    async def set_turn(task_id: str, body: TurnIn) -> TaskOut:
        return TaskOut.model_validate(service.set_turn(task_id, body.turn))

    @app.put("/tasks/{task_id}/blocked")
    async def set_blocked(task_id: str, body: BlockedIn) -> TaskOut:
        return TaskOut.model_validate(service.set_blocked(task_id, body.blocked))

    @app.put("/tasks/{task_id}/claim")
    async def claim(task_id: str, body: ClaimIn) -> TaskOut:
        try:  # a runner claims an unclaimed task before spawning its container (ADR 0008)
            task = service.claim(task_id, body.runner_id)
        except AlreadyClaimed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TaskOut.model_validate(task)

    @app.delete("/tasks/{task_id}/claim")
    async def release(task_id: str) -> TaskOut:
        return TaskOut.model_validate(service.release(task_id))

    @app.put("/tasks/{task_id}/provisioning")
    async def record_provisioning(task_id: str, body: ProvisioningIn) -> TaskOut:
        try:  # the session service reports the host branch + per-task clone it created (ADR 0011)
            task = service.record_provisioning(
                task_id, branch=body.branch, clone=body.clone
            )
        except ValueError as exc:  # slug not set yet
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TaskOut.model_validate(task)

    # -- artifacts ----------------------------------------------------------------

    @app.put("/tasks/{task_id}/artifacts/{name}", status_code=204)
    async def put_artifact(task_id: str, name: str, request: Request) -> Response:
        service.put_artifact(task_id, name, await request.body())
        return Response(status_code=204)

    @app.get("/tasks/{task_id}/artifacts")
    async def list_artifacts(task_id: str) -> list[str]:
        return service.list_artifacts(task_id)

    @app.get("/tasks/{task_id}/artifacts/{name}")
    async def get_artifact(task_id: str, name: str) -> Response:
        content = service.get_artifact(task_id, name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"artifact {name!r} not found")
        return Response(content=content, media_type="application/octet-stream")

    # -- liveness -----------------------------------------------------------------

    @app.post("/tasks/{task_id}/registrations", status_code=201)
    async def register(task_id: str, body: RegisterIn) -> RegistrationOut:
        return RegistrationOut.model_validate(
            service.register(task_id, body.container_id, body.runner_id)
        )

    @app.get("/tasks/{task_id}/registrations")
    async def list_registrations(task_id: str) -> list[RegistrationOut]:
        service.get_task(task_id)  # 404 if the task is unknown
        return [RegistrationOut.model_validate(r) for r in service.registrations(task_id)]

    @app.post("/registrations/{registration_id}/heartbeat")
    async def heartbeat(registration_id: str) -> RegistrationOut:
        return RegistrationOut.model_validate(service.heartbeat(registration_id))

    @app.delete("/registrations/{registration_id}", status_code=204)
    async def deregister(registration_id: str) -> Response:
        service.deregister(registration_id)
        return Response(status_code=204)

    app.mount("/mcp", mcp_app)  # in-container agents connect here for task operations + artifacts
    return app
