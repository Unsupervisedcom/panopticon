"""The MCP server (ADR 0003/0006): the task service hosts MCP so in-container agents reach it —
task **operations as tools**, **artifacts as resources** — over the same task service the REST
clients use. Built on the official MCP SDK (FastMCP).

LLM-free: this is the *surface* the agent calls; no LLM runs here (the determinism invariant).
`build_mcp_server` returns the server (exercised in-memory in tests); `create_app` mounts its
streamable-HTTP app at ``/mcp`` so the same control plane serves REST and MCP, and the
in-container agent launcher points claude at it (`container/agent.py`).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from panopticon.core.artifacts import mcp_uri
from panopticon.core.models import Actor, Status
from panopticon.taskservice.api import TaskOut
from panopticon.taskservice.service import TaskService

#: The artifact resource URI template (the shared id→URI resolver, ADR 0003).
ARTIFACT_URI = "panopticon://tasks/{task_id}/artifacts/{name}"


def _task(task: object) -> dict[str, Any]:
    """Serialize a Task the same way the REST API does, so both surfaces agree."""
    return TaskOut.model_validate(task).model_dump(mode="json")


def build_mcp_server(service: TaskService, *, name: str = "panopticon") -> FastMCP:
    """An MCP server exposing the task service's agent-facing operations + artifacts."""
    # Disable the SDK's DNS-rebinding (Host/Origin) guard: the agent reaches us across the
    # container→host boundary (e.g. ``host.docker.internal``), not just localhost. The control
    # plane is on a trusted network; per-task authorization is tracked separately (BACKLOG).
    mcp = FastMCP(name, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @mcp.tool(description="Fetch a task: state, turn, blocked, slug, and history.")
    async def get_task(task_id: str) -> dict[str, Any]:
        return _task(await service.get_task(task_id))

    @mcp.tool(description="Set the task's human-readable slug.")
    async def set_slug(task_id: str, slug: str) -> dict[str, Any]:
        return _task(await service.set_slug(task_id, slug))

    @mcp.tool(description="Record an external URL for the task (e.g. its PR); the dashboard's 'p' hotkey opens it.")
    async def set_url(task_id: str, url: str) -> dict[str, Any]:
        return _task(await service.set_url(task_id, url))

    @mcp.tool(description="Record the cumulative tokens claude in this container has used.")
    async def set_tokens_used(task_id: str, tokens_used: int) -> dict[str, Any]:
        return _task(await service.set_tokens_used(task_id, tokens_used))

    @mcp.tool(description="Record an estimate of the total tokens this task will consume (set when planning).")
    async def set_token_estimate(task_id: str, token_estimate: int) -> dict[str, Any]:
        return _task(await service.set_token_estimate(task_id, token_estimate))

    @mcp.tool(description="Apply a named core operation (e.g. 'advance', 'drop').")
    async def apply_operation(task_id: str, operation: str) -> dict[str, Any]:
        return _task(await service.apply_operation(task_id, operation))

    @mcp.tool(description="Move the task to any state directly (free move; bypasses the gate).")
    async def set_state(task_id: str, state: str) -> dict[str, Any]:
        return _task(await service.set_state(task_id, state))

    @mcp.tool(
        description="Resolve one promised responsibility ('met', or 'failed' with a comment)."
    )
    async def resolve_responsibility(
        task_id: str, key: str, status: str, comment: str | None = None
    ) -> dict[str, Any]:
        return _task(await service.resolve_responsibility(task_id, key, status=Status(status), comment=comment))

    @mcp.tool(description="Flip who holds the turn: 'user' or 'agent'.")
    async def set_turn(task_id: str, turn: str) -> dict[str, Any]:
        return _task(await service.set_turn(task_id, Actor(turn)))

    @mcp.tool(description="Set or clear the deliberate 'blocked' marker (survives turn flips).")
    async def set_blocked(task_id: str, blocked: bool) -> dict[str, Any]:
        return _task(await service.set_blocked(task_id, blocked))

    @mcp.tool(
        description=(
            "Replace the task's dependency list with the given task IDs. "
            "Each ID must reference an existing task; pass an empty list to clear all dependencies. "
            "Dependencies are tracking only — the state machine does not enforce them."
        )
    )
    async def set_dependencies(task_id: str, dep_ids: list[str]) -> dict[str, Any]:
        try:
            return _task(await service.set_dependencies(task_id, dep_ids))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    # -- orchestration (gated to workflows whose `orchestrates` is set) -----------------------
    # These widen an agent beyond its own task — creating tasks and discovering workflows — so
    # each takes the acting orchestrator task's id and the service authorizes it against that
    # task's workflow. The per-task tools above already accept any task_id, so seeding a child
    # (set_slug/put_artifact/resolve_responsibility/set_turn) needs nothing new.

    @mcp.tool(
        description=(
            "Create a new task on behalf of an orchestrator task (gated to orchestration "
            "workflows). The task is created in your own repo. Pass your own task id as "
            "orchestrator_task_id. The `memo` is a brief, one-line reminder of what the task "
            "is (shown in the dashboard) — not a full description; the full description goes "
            "in the task's plan.md. `initial_prompt` (optional) is passed as Claude's first "
            "message on first spawn — the agent starts autonomously without waiting for user "
            "input, e.g. \"review your plan\". `artifacts` "
            "(optional) is a name→content map of artifacts to write immediately (e.g. "
            "{\"plan.md\": \"...\"}) — written before the call returns so the spawner always "
            "finds them present. The new task's governor_task_id is set to orchestrator_task_id "
            "automatically. Returns the new task."
        )
    )
    async def create_task(
        orchestrator_task_id: str,
        workflow: str,
        memo: str | None = None,
        initial_prompt: str | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return _task(
            await service.create_task_as(
                orchestrator_task_id,
                workflow,
                memo=memo,
                initial_prompt=initial_prompt,
                artifacts=artifacts,
            )
        )

    @mcp.tool(description="List workflow names (gated to orchestration workflows); pass your own task id as orchestrator_task_id.")
    async def list_workflows(orchestrator_task_id: str) -> list[str]:
        return await service.workflow_names_as(orchestrator_task_id)

    @mcp.tool(description="Write (create or overwrite) a task artifact, e.g. the plan. Returns its URI.")
    async def put_artifact(task_id: str, name: str, content: str) -> str:
        await service.put_artifact(task_id, name, content.encode())
        return mcp_uri(task_id, name)

    @mcp.tool(
        description=(
            "List a task's artifacts: each name and its canonical MCP URI (read the URI as a "
            "resource to fetch the contents). The read resource is a non-enumerable URI "
            "template, so this is how you discover artifacts you did not write yourself."
        )
    )
    async def list_artifacts(task_id: str) -> list[dict[str, str]]:
        names = await service.list_artifacts(task_id)
        return [{"name": name, "uri": mcp_uri(task_id, name)} for name in names]

    @mcp.resource(ARTIFACT_URI, description="A task's file-backed artifact (plan, notes).")
    async def artifact(task_id: str, name: str) -> str:
        data = await service.get_artifact(task_id, name)
        if data is None:
            raise FileNotFoundError(f"no artifact {name!r} for task {task_id!r}")
        return data.decode()

    return mcp
