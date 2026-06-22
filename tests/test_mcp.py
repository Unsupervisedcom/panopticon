"""The MCP server surface — tools + artifact resources — exercised in-memory via the MCP
client (no LLM, no HTTP). The HTTP hosting is mounted on the runnable server (Slice 7a)."""

from __future__ import annotations

from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session as connect

from panopticon.core.models import Repo
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.mcp import build_mcp_server
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


def _service(tmp_path: Path) -> TaskService:
    svc = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


async def test_tools_are_exposed_and_drive_the_task(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    task = svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert {
            "get_task", "set_slug", "set_url", "apply_operation", "set_state",
            "resolve_responsibility", "set_turn", "set_blocked", "put_artifact",
        } <= names
        result = await s.call_tool("apply_operation", {"task_id": task.id, "operation": "advance"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["state"] == "COMPLETE"
    assert svc.get_task(task.id).state == "COMPLETE"  # the tool actually mutated the task


async def test_artifacts_round_trip_via_tool_and_resource(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    task = svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool("put_artifact", {"task_id": task.id, "name": "plan.md", "content": "# Plan"})
        res = await s.read_resource(f"panopticon://tasks/{task.id}/artifacts/plan.md")
        assert res.contents[0].text == "# Plan"  # type: ignore[union-attr]
    assert svc.get_artifact(task.id, "plan.md") == b"# Plan"


async def test_set_turn_via_tool(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    task = svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("set_turn", {"task_id": task.id, "turn": "user"})
        assert result.structuredContent is not None
        assert result.structuredContent["turn"] == "user"


async def test_set_url_via_tool(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    task = svc.create_task("r1", "spike")
    url = "https://github.com/acme/widgets/pull/7"
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("set_url", {"task_id": task.id, "url": url})
        assert result.structuredContent is not None
        assert result.structuredContent["url"] == url
    assert svc.get_task(task.id).url == url  # the tool actually mutated the task
