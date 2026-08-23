"""The MCP server surface — tools + artifact resources — exercised in-memory via the MCP
client (no LLM, no HTTP). The HTTP hosting is mounted on the runnable server (Slice 7a)."""

from __future__ import annotations

import base64
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session as connect

from panopticon.core.models import Actor, Repo
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.mcp import build_mcp_server
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import GithubSelfReviewed, Orchestrator, Spike


async def _service(tmp_path: Path) -> TaskService:
    svc = TaskService(
        SqlAlchemyStore(),
        {
            "spike": Spike(),
            "orchestrator": Orchestrator(),
            "github-self-reviewed": GithubSelfReviewed(),
        },
        FilesystemArtifactStore(tmp_path),
    )
    await svc.init()
    await svc.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            enabled_workflows=["github-self-reviewed"],
        )
    )
    await svc.create_repo(
        Repo(
            id="r2",
            name="acme/other",
            git_url="https://x/r2.git",
            enabled_workflows=["github-self-reviewed"],
        )
    )
    return svc


async def test_tools_are_exposed_and_drive_the_task(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert {
            "get_task",
            "set_slug",
            "set_url",
            "apply_operation",
            "set_state",
            "resolve_responsibility",
            "set_turn",
            "set_blocked",
            "set_sort_weight",
            "put_artifact",
            "list_artifacts",
        } <= names
        result = await s.call_tool("apply_operation", {"task_id": task.id, "operation": "advance"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["state"] == "COMPLETE"
    assert (await svc.get_task(task.id)).state == "COMPLETE"  # the tool actually mutated the task


async def test_artifacts_round_trip_via_tool_and_resource(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool(
            "put_artifact", {"task_id": task.id, "name": "plan.md", "content": "# Plan"}
        )
        res = await s.read_resource(f"panopticon://tasks/{task.id}/artifacts/plan.md")
        assert res.contents[0].text == "# Plan"  # type: ignore[union-attr]
    assert await svc.get_artifact(task.id, "plan.md") == b"# Plan"


async def test_binary_artifact_round_trips_via_base64_tool_and_blob_resource(
    tmp_path: Path,
) -> None:
    # A screenshot's bytes aren't valid UTF-8: write via content_base64, read back as a base64 blob
    # (JSON can't carry raw bytes, so the resource returns a BlobResourceContents, not text).
    png = b"\x89PNG\r\n\x1a\n\x00\xff\xfe\x01binary\x00data"
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool(
            "put_artifact",
            {
                "task_id": task.id,
                "name": "shot.png",
                "content_base64": base64.b64encode(png).decode(),
            },
        )
        res = await s.read_resource(f"panopticon://tasks/{task.id}/artifacts/shot.png")
        blob = res.contents[0].blob  # type: ignore[union-attr]
        assert base64.b64decode(blob) == png
    assert await svc.get_artifact(task.id, "shot.png") == png


async def test_put_artifact_requires_exactly_one_content_argument(tmp_path: Path) -> None:
    # Exactly one of content / content_base64 — neither (nor both) is a usage error.
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        neither = await s.call_tool("put_artifact", {"task_id": task.id, "name": "x"})
        assert neither.isError is True
        both = await s.call_tool(
            "put_artifact",
            {"task_id": task.id, "name": "x", "content": "hi", "content_base64": "aGk="},
        )
        assert both.isError is True


async def test_list_artifacts_returns_names_and_readable_uris(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool(
            "put_artifact", {"task_id": task.id, "name": "plan.md", "content": "# Plan"}
        )
        await s.call_tool(
            "put_artifact", {"task_id": task.id, "name": "notes.md", "content": "notes"}
        )

        result = await s.call_tool("list_artifacts", {"task_id": task.id})
        assert result.isError is False
        listed = result.structuredContent["result"]  # type: ignore[index]
        by_name = {entry["name"]: entry["uri"] for entry in listed}
        assert set(by_name) == {"plan.md", "notes.md"}
        assert by_name["plan.md"] == f"panopticon://tasks/{task.id}/artifacts/plan.md"

        # the listed URI is the real, readable resource — the path that failed before this tool.
        res = await s.read_resource(by_name["plan.md"])
        assert res.contents[0].text == "# Plan"  # type: ignore[union-attr]


async def test_artifact_with_spaces_in_name_round_trips_over_mcp(tmp_path: Path) -> None:
    # A name with spaces/reserved chars must list with a valid (percent-encoded) URI that reads
    # back — the resource handler decodes the segment the MCP layer captures encoded.
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike", artifacts={"my notes.md": "hello world"})
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("list_artifacts", {"task_id": task.id})
        listed = result.structuredContent["result"]  # type: ignore[index]
        uri = {entry["name"]: entry["uri"] for entry in listed}["my notes.md"]
        assert uri == f"panopticon://tasks/{task.id}/artifacts/my%20notes.md"
        res = await s.read_resource(uri)
        assert res.contents[0].text == "hello world"  # type: ignore[union-attr]


async def test_list_artifacts_is_empty_when_none(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("list_artifacts", {"task_id": task.id})
        assert result.isError is False
        assert result.structuredContent["result"] == []  # type: ignore[index]


async def test_dotfile_artifacts_are_stored_and_listed(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool(
            "put_artifact", {"task_id": task.id, "name": ".hidden", "content": "secret"}
        )
        result = await s.call_tool("list_artifacts", {"task_id": task.id})
        assert result.isError is False
        listed = result.structuredContent["result"]  # type: ignore[index]
        by_name = {entry["name"]: entry["uri"] for entry in listed}
        assert ".hidden" in by_name
        assert by_name[".hidden"] == f"panopticon://tasks/{task.id}/artifacts/.hidden"
        res = await s.read_resource(by_name[".hidden"])
        assert res.contents[0].text == "secret"  # type: ignore[union-attr]


async def test_set_turn_via_tool(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("set_turn", {"task_id": task.id, "turn": "user"})
        assert result.structuredContent is not None
        assert result.structuredContent["turn"] == "user"


async def test_set_url_via_tool(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    url = "https://github.com/acme/widgets/pull/7"
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("set_url", {"task_id": task.id, "url": url})
        assert result.structuredContent is not None
        assert result.structuredContent["url"] == url
    assert (await svc.get_task(task.id)).url == url  # the tool actually mutated the task


async def test_set_sort_weight_via_tool(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("set_sort_weight", {"task_id": task.id, "sort_weight": 7})
        assert result.structuredContent is not None
        assert result.structuredContent["sort_weight"] == 7
    assert (await svc.get_task(task.id)).sort_weight == 7  # the tool actually mutated the task


# -- orchestration tools (gated to workflows whose `orchestrates` is set) --------------------


async def test_orchestration_tools_are_exposed(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"create_task", "list_workflows"} <= names


async def test_orchestrator_creates_in_its_own_repo(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    boss = await svc.create_task("r2", "orchestrator")  # the orchestrator lives in r2
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        wfs = await s.call_tool("list_workflows", {"orchestrator_task_id": boss.id})
        assert "github-self-reviewed" in wfs.structuredContent["result"]  # type: ignore[index]

        result = await s.call_tool(
            "create_task",
            {"orchestrator_task_id": boss.id, "workflow": "github-self-reviewed"},
        )
        assert result.isError is False
        child_id = result.structuredContent["id"]  # type: ignore[index]
        assert result.structuredContent["state"] == "PLANNING"  # type: ignore[index]
    child = await svc.get_task(child_id)
    assert child.workflow == "github-self-reviewed"  # the tool really created it
    assert child.repo_id == "r2"  # in the orchestrator's own repo, not some other repo


async def test_create_task_as_sets_governor_task_id(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool(
            "create_task",
            {"orchestrator_task_id": boss.id, "workflow": "spike"},
        )
        assert result.isError is False
        child_id = result.structuredContent["id"]  # type: ignore[index]
    child = await svc.get_task(child_id)
    assert child.governor_task_id == boss.id  # auto-wired to the orchestrator


async def test_create_task_seeds_sort_weight(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool(
            "create_task",
            {"orchestrator_task_id": boss.id, "workflow": "spike", "sort_weight": 9},
        )
        assert result.isError is False
        assert result.structuredContent["sort_weight"] == 9  # type: ignore[index]
        child_id = result.structuredContent["id"]  # type: ignore[index]
    assert (await svc.get_task(child_id)).sort_weight == 9  # persisted


async def test_create_task_carries_agent_cli_override(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool(
            "create_task",
            {"orchestrator_task_id": boss.id, "workflow": "spike", "agent_cli": "codex"},
        )
        assert result.isError is False
        assert result.structuredContent["agent_cli"] == "codex"  # type: ignore[index]
        child_id = result.structuredContent["id"]  # type: ignore[index]
    assert (await svc.get_task(child_id)).agent_cli == "codex"  # persisted as the per-task override


async def test_create_task_rejected_for_non_orchestrator(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    task = await svc.create_task("r1", "spike")  # spike does not orchestrate
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        for tool in ("create_task", "list_workflows"):
            args = {"orchestrator_task_id": task.id}
            if tool == "create_task":
                args |= {"workflow": "spike"}
            result = await s.call_tool(tool, args)
            assert result.isError is True  # the gate holds: a non-orchestrator may not orchestrate
    assert len(await svc.list_tasks()) == 1  # nothing was created


async def test_create_task_with_initial_prompt_and_artifacts(tmp_path: Path) -> None:
    """create_task with initial_prompt and artifacts writes both atomically."""
    svc = await _service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool(
            "create_task",
            {
                "orchestrator_task_id": boss.id,
                "workflow": "github-self-reviewed",
                "memo": "Add a /healthz endpoint",
                "initial_prompt": "review your plan",
                "artifacts": {"plan.md": "# Plan\nDo the thing."},
            },
        )
        assert result.isError is False
        child_id = result.structuredContent["id"]  # type: ignore[index]

    child = await svc.get_task(child_id)
    assert child.initial_prompt == "review your plan"
    # plan.md is present immediately — no separate put_artifact call needed
    assert await svc.get_artifact(child_id, "plan.md") == b"# Plan\nDo the thing."


async def test_orchestrator_seeds_a_child_ready_to_approve(tmp_path: Path) -> None:
    """The motivating end-to-end: create a github-self-reviewed task with the plan inline —
    plan.md written, `plan-written` met, turn handed to the user."""
    svc = await _service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        created = await s.call_tool(
            "create_task",
            {
                "orchestrator_task_id": boss.id,
                "workflow": "github-self-reviewed",
                "memo": "Add a /healthz endpoint",
                "initial_prompt": "review your plan",
                "artifacts": {"plan.md": "# Plan\n..."},
            },
        )
        child_id = created.structuredContent["id"]  # type: ignore[index]
        await s.call_tool("set_slug", {"task_id": child_id, "slug": "add-healthz"})
        await s.call_tool(
            "resolve_responsibility",
            {"task_id": child_id, "key": "plan-written", "status": "met"},
        )
        await s.call_tool("set_turn", {"task_id": child_id, "turn": "user"})

    child = await svc.get_task(child_id)
    assert child.state == "PLANNING"  # still in planning, awaiting the user's approval
    assert child.slug == "add-healthz"
    assert child.turn is Actor.USER  # handed to the user to review/advance
    assert child.outstanding_responsibilities == []  # the gate is clear — the user can advance
    assert await svc.get_artifact(child_id, "plan.md") == b"# Plan\n..."
