"""The MCP surface contract (definition only in this slice).

Agents reach the task service primarily over MCP (ADR 0003/0006): artifacts as
**resources**, task operations as **tools**. This module pins the contract — resource URI
scheme and tool names/shapes — so the surface is agreed now. The running MCP server is
wired when real containers connect (Slice 2); the walking skeleton uses the REST API.
"""

from __future__ import annotations

from dataclasses import dataclass

from panopticon.core.artifacts import MCP_URI_SCHEME, mcp_uri

__all__ = ["MCP_URI_SCHEME", "mcp_uri", "ToolSpec", "TASK_TOOLS"]


@dataclass(frozen=True)
class ToolSpec:
    """A tool the agent may call, with its JSON-style argument names."""

    name: str
    description: str
    arguments: tuple[str, ...]


#: Core task operations exposed to in-container agents as MCP tools. Workflow-specific
#: skills are layered on top per the active workflow (ADR 0004) and are not listed here.
TASK_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("get_task", "Fetch the current task (state, turn, history).", ("task_id",)),
    ToolSpec("set_slug", "Set the task's human-readable slug if unset.", ("task_id", "slug")),
    ToolSpec(
        "request_transition",
        "Request a workflow transition; the service enforces legality and the gate.",
        ("task_id", "to_state", "trigger"),
    ),
    ToolSpec(
        "resolve_responsibility",
        "Resolve one of the current state's promised responsibilities (MET or FAILED).",
        ("task_id", "key", "status", "comment"),
    ),
    ToolSpec("put_artifact", "Create or update a task artifact (e.g. plan).", ("task_id", "name", "content")),
    ToolSpec("get_artifact", "Read a task artifact.", ("task_id", "name")),
    ToolSpec("heartbeat", "Refresh this container's liveness registration.", ("registration_id",)),
)
