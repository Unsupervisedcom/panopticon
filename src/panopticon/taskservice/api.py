"""The task service REST API (FastAPI).

The dashboard, the runner, and in-container skills are clients of this API. (Agents also
reach artifacts/tools over MCP — see :mod:`panopticon.taskservice.mcp` — but the walking
skeleton uses REST.) ``create_app`` builds an app around an injected
:class:`~panopticon.taskservice.service.TaskService`, so tests can wire a deterministic one.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from panopticon.core.artifacts import ArtifactError
from panopticon.core.models import Actor, Repo, Status
from panopticon.core.store import AlreadyExists, NotFound, StoreError
from panopticon.core.workflow import IllegalTransition, InvalidWorkflow, ResponsibilitiesNotMet
from panopticon.taskservice.service import TaskService, UnknownWorkflow

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
    slug: str | None
    history: list[HistoryOut]


class RepoIn(BaseModel):
    id: str
    name: str
    git_url: str
    default_base: str = "main"


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    default_base: str


class CreateTaskIn(BaseModel):
    repo_id: str
    workflow: str


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


def create_app(service: TaskService) -> FastAPI:
    app = FastAPI(title="panopticon task service", version="0.0.1")

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

    # -- tasks --------------------------------------------------------------------

    @app.post("/tasks", status_code=201)
    async def create_task(body: CreateTaskIn) -> TaskOut:
        return TaskOut.model_validate(service.create_task(body.repo_id, body.workflow))

    @app.get("/tasks")
    async def list_tasks() -> list[TaskOut]:
        return [TaskOut.model_validate(t) for t in service.list_tasks()]

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

    return app
