"""The MCP server (ADR 0003/0006): the task service hosts MCP so in-container agents reach it —
task **operations as tools**, **artifacts as resources** — over the same task service the REST
clients use. Built on the official MCP SDK (FastMCP).

LLM-free: this is the *surface* the agent calls; no LLM runs here (the determinism invariant).
`build_mcp_server` returns the server (exercised in-memory in tests); `create_app` mounts its
streamable-HTTP app at ``/mcp`` so the same control plane serves REST and MCP, and the
in-container agent launcher points claude at it (`container/agent.py`).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from panopticon.core.artifacts import (
    decode_b64_artifact,
    decode_segment,
    mcp_uri,
    repo_mcp_uri,
)
from panopticon.core.models import Actor, Status
from panopticon.taskservice.api import TaskOut
from panopticon.taskservice.service import TaskService

_log = logging.getLogger(__name__)

#: The artifact resource URI template (the shared id→URI resolver, ADR 0003).
ARTIFACT_URI = "panopticon://tasks/{task_id}/artifacts/{name}"

#: The **repo** artifact resource URI template — the repo-scoped twin of :data:`ARTIFACT_URI`.
#: A nested name (``notes/api.md``) arrives percent-encoded (``notes%2Fapi.md``) from
#: :func:`repo_mcp_uri`, so it still occupies the single segment the template matches.
REPO_ARTIFACT_URI = "panopticon://repos/{repo_id}/artifacts/{name}"


def _task(task: object) -> dict[str, Any]:
    """Serialize a Task the same way the REST API does, so both surfaces agree."""
    return TaskOut.model_validate(task).model_dump(mode="json")


def build_mcp_server(service: TaskService, *, name: str = "panopticon") -> FastMCP:
    """An MCP server exposing the task service's agent-facing operations + artifacts."""
    # Disable the SDK's DNS-rebinding (Host/Origin) guard: the agent reaches us across the
    # container→host boundary (e.g. ``host.docker.internal``), not just localhost. The control
    # plane is on a trusted network; per-task authorization is tracked separately (BACKLOG).
    mcp = FastMCP(
        name, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )

    @mcp.tool(description="Fetch a task: state, turn, blocked, slug, and history.")
    async def get_task(task_id: str) -> dict[str, Any]:
        return _task(await service.get_task(task_id))

    @mcp.tool(description="Set the task's human-readable slug.")
    async def set_slug(task_id: str, slug: str) -> dict[str, Any]:
        return _task(await service.set_slug(task_id, slug))

    @mcp.tool(
        description="Record an external URL for the task (e.g. its PR); the dashboard's 'p' hotkey opens it."
    )
    async def set_url(task_id: str, url: str) -> dict[str, Any]:
        return _task(await service.set_url(task_id, url))

    @mcp.tool(
        description=(
            "Ask the session service to push this task's merge back to origin: the task branch "
            "first (a backup that always lands), then `branch` (the base branch you merged into). "
            "The container cannot do this itself when origin is a local path, so the host does it "
            "and reports back — poll `get_task` and read the `push` field for the outcome "
            "('pushed', 'partial' = only the backup landed, or 'failed', with a `detail`)."
        )
    )
    async def request_push(task_id: str, branch: str) -> dict[str, Any]:
        _log.debug("mcp request_push task=%s branch=%s", task_id, branch)
        return _task(await service.request_push(task_id, branch=branch))

    @mcp.tool(description="Apply a named core operation (e.g. 'advance', 'drop').")
    async def apply_operation(task_id: str, operation: str) -> dict[str, Any]:
        _log.debug("mcp apply_operation task=%s operation=%s", task_id, operation)
        return _task(await service.apply_operation(task_id, operation))

    @mcp.tool(description="Move the task to any state directly (free move; bypasses the gate).")
    async def set_state(task_id: str, state: str) -> dict[str, Any]:
        _log.debug("mcp set_state task=%s state=%s", task_id, state)
        return _task(await service.set_state(task_id, state))

    @mcp.tool(
        description="Resolve one promised responsibility ('met', or 'failed' with a comment)."
    )
    async def resolve_responsibility(
        task_id: str, key: str, status: str, comment: str | None = None
    ) -> dict[str, Any]:
        _log.debug("mcp resolve_responsibility task=%s key=%s status=%s", task_id, key, status)
        return _task(
            await service.resolve_responsibility(
                task_id, key, status=Status(status), comment=comment
            )
        )

    @mcp.tool(description="Flip who holds the turn: 'user' or 'agent'.")
    async def set_turn(task_id: str, turn: str) -> dict[str, Any]:
        return _task(await service.set_turn(task_id, Actor(turn)))

    @mcp.tool(description="Set or clear the deliberate 'blocked' marker (survives turn flips).")
    async def set_blocked(task_id: str, blocked: bool) -> dict[str, Any]:
        return _task(await service.set_blocked(task_id, blocked))

    @mcp.tool(
        description=(
            "Set the task's dashboard sort weight (default 0; higher sorts first). "
            "Ranks above the last-updated timestamp but below state/turn."
        )
    )
    async def set_sort_weight(task_id: str, sort_weight: int) -> dict[str, Any]:
        return _task(await service.set_sort_weight(task_id, sort_weight))

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
            "orchestrator_task_id. The `memo` is a brief reminder of what the task "
            "is (shown in the dashboard) — not a full description; the full description goes "
            "in the task's plan.md. `initial_prompt` (optional) is prefilled as the agent's first "
            "prompt on first spawn — the agent starts autonomously without waiting for user "
            'input, e.g. "review your plan". `artifacts` '
            "(optional) is a name→content map of text artifacts to write immediately (e.g. "
            '{"plan.md": "..."}) — written before the call returns so the spawner always '
            "finds them present. `artifacts_b64` (optional) is the same for binary artifacts "
            "(e.g. a screenshot), name→base64. The new task's governor_task_id is set to "
            "orchestrator_task_id automatically. `sort_weight` (optional, default 0) is the "
            "task's dashboard sort priority — higher sorts first, ranking above the last-updated "
            "timestamp but below state/turn. `agent_cli` (optional) overrides which agent CLI the "
            "task runs; omit to use the repo's default. Returns the new task."
        )
    )
    async def create_task(
        orchestrator_task_id: str,
        workflow: str,
        memo: str | None = None,
        initial_prompt: str | None = None,
        artifacts: dict[str, str] | None = None,
        artifacts_b64: dict[str, str] | None = None,
        sort_weight: int = 0,
        agent_cli: str | None = None,
    ) -> dict[str, Any]:
        _log.debug("mcp create_task orchestrator=%s workflow=%s", orchestrator_task_id, workflow)
        return _task(
            await service.create_task_as(
                orchestrator_task_id,
                workflow,
                memo=memo,
                initial_prompt=initial_prompt,
                artifacts=artifacts,
                artifacts_b64=artifacts_b64,
                sort_weight=sort_weight,
                agent_cli=agent_cli,
            )
        )

    @mcp.tool(
        description="List workflow names (gated to orchestration workflows); pass your own task id as orchestrator_task_id."
    )
    async def list_workflows(orchestrator_task_id: str) -> list[str]:
        return await service.workflow_names_as(orchestrator_task_id)

    @mcp.tool(
        description=(
            "Write (create or overwrite) a task artifact, e.g. the plan. Returns its URI. "
            "Pass text in `content`; for a binary artifact (e.g. a screenshot) pass base64 in "
            "`content_base64` instead — supply exactly one. For a large binary the more efficient "
            "path is the REST endpoint (PUT /tasks/{id}/artifacts/{name} with the raw bytes), which "
            "keeps the base64 out of your context."
        )
    )
    async def put_artifact(
        task_id: str, name: str, content: str | None = None, content_base64: str | None = None
    ) -> str:
        if content is not None and content_base64 is None:
            data = content.encode()
        elif content is None and content_base64 is not None:
            data = decode_b64_artifact(name, content_base64)
        else:
            raise ValueError("provide exactly one of `content` or `content_base64`")
        await service.put_artifact(task_id, name, data)
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

    @mcp.tool(
        description=(
            "Write (create or overwrite) an artifact on **your repo** rather than your task, and "
            "return its URI. Repo artifacts are shared by every task in this repo and outlive "
            "yours, so this is where durable, repo-wide material belongs: conventions and gotchas "
            "worth passing on, accumulated notes, reference screenshots. Task artifacts (the plan) "
            "stay on the task. The repo is your own — it is resolved from `task_id`, so pass your "
            "own task id. `name` may name subdirectories ('notes/api.md'); pass text in `content` "
            "or base64 in `content_base64` (exactly one). For a large binary the more efficient "
            "path is the REST endpoint (PUT /repos/{repo_id}/artifacts/{name} with the raw bytes), "
            "which keeps the base64 out of your context."
        )
    )
    async def put_repo_artifact(
        task_id: str, name: str, content: str | None = None, content_base64: str | None = None
    ) -> str:
        if content is not None and content_base64 is None:
            data = content.encode()
        elif content is None and content_base64 is not None:
            data = decode_b64_artifact(name, content_base64)
        else:
            raise ValueError("provide exactly one of `content` or `content_base64`")
        repo_id = await service.put_repo_artifact_for_task(task_id, name, data)
        return repo_mcp_uri(repo_id, name)

    @mcp.tool(
        description=(
            "List your repo's artifacts — the documents shared across every task in this repo — "
            "each name and its canonical MCP URI (read the URI as a resource to fetch the "
            "contents). Pass your own task id; the repo is resolved from it. Names may include "
            "subdirectories. Read this before writing repo material, so you extend what is there "
            "rather than duplicating it."
        )
    )
    async def list_repo_artifacts(task_id: str) -> list[dict[str, str]]:
        repo_id, names = await service.list_repo_artifacts_for_task(task_id)
        return [{"name": name, "uri": repo_mcp_uri(repo_id, name)} for name in names]

    @mcp.resource(
        REPO_ARTIFACT_URI,
        description="A repo's file-backed artifact, shared by every task in that repo.",
    )
    async def repo_artifact(repo_id: str, name: str) -> str | bytes:
        # Same encoding contract as the task resource: the captured segments arrive
        # percent-encoded, and for a repo artifact that includes a nested name's separators
        # (``notes%2Fapi.md`` → ``notes/api.md``).
        repo_id, name = decode_segment(repo_id), decode_segment(name)
        data = await service.get_repo_artifact(repo_id, name)
        if data is None:
            raise FileNotFoundError(f"no artifact {name!r} for repo {repo_id!r}")
        try:
            return data.decode()
        except UnicodeDecodeError:
            return data

    @mcp.resource(ARTIFACT_URI, description="A task's file-backed artifact (plan, notes).")
    async def artifact(task_id: str, name: str) -> str | bytes:
        # The MCP layer captures the URI-template segments without percent-decoding them, so a name
        # with spaces/reserved chars arrives encoded (``my%20notes.md``). Reverse mcp_uri's encoding.
        task_id, name = decode_segment(task_id), decode_segment(name)
        data = await service.get_artifact(task_id, name)
        if data is None:
            raise FileNotFoundError(f"no artifact {name!r} for task {task_id!r}")
        # Text artifacts return as ``str``; binary ones (a non-UTF-8 screenshot/PDF) return as
        # ``bytes``, which the SDK serves as a base64 BlobResourceContents. JSON can't carry raw
        # bytes, so this is the only way a binary artifact reads back over MCP.
        try:
            return data.decode()
        except UnicodeDecodeError:
            return data

    return mcp
