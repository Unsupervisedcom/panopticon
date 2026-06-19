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
    def get_task(task_id: str) -> dict[str, Any]:
        return _task(service.get_task(task_id))

    @mcp.tool(description="Set the task's human-readable slug.")
    def set_slug(task_id: str, slug: str) -> dict[str, Any]:
        return _task(service.set_slug(task_id, slug))

    @mcp.tool(description="Apply a named core operation (e.g. 'advance', 'drop').")
    def apply_operation(task_id: str, operation: str) -> dict[str, Any]:
        return _task(service.apply_operation(task_id, operation))

    @mcp.tool(description="Move the task to any state directly (free move; bypasses the gate).")
    def set_state(task_id: str, state: str) -> dict[str, Any]:
        return _task(service.set_state(task_id, state))

    @mcp.tool(
        description="Resolve one promised responsibility ('met', or 'failed' with a comment)."
    )
    def resolve_responsibility(
        task_id: str, key: str, status: str, comment: str | None = None
    ) -> dict[str, Any]:
        return _task(service.resolve_responsibility(task_id, key, status=Status(status), comment=comment))

    @mcp.tool(description="Flip who holds the turn: 'user' or 'agent'.")
    def set_turn(task_id: str, turn: str) -> dict[str, Any]:
        return _task(service.set_turn(task_id, Actor(turn)))

    @mcp.tool(description="Set or clear the deliberate 'blocked' marker (survives turn flips).")
    def set_blocked(task_id: str, blocked: bool) -> dict[str, Any]:
        return _task(service.set_blocked(task_id, blocked))

    @mcp.tool(description="Write (create or overwrite) a task artifact, e.g. the plan. Returns its URI.")
    def put_artifact(task_id: str, name: str, content: str) -> str:
        service.put_artifact(task_id, name, content.encode())
        return mcp_uri(task_id, name)

    @mcp.resource(ARTIFACT_URI, description="A task's file-backed artifact (plan, notes).")
    def artifact(task_id: str, name: str) -> str:
        data = service.get_artifact(task_id, name)
        if data is None:
            raise FileNotFoundError(f"no artifact {name!r} for task {task_id!r}")
        return data.decode()

    return mcp
