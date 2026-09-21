"""The tarot authoring passthroughs, over MCP — the surface the in-container agent actually calls.

Tarot's authoring skill has seven steps; four invoke the CLI, and these are those four, run
host-side against the task's clone. Exercised in-memory through the MCP client (no LLM, no HTTP),
with a fake command-runner standing in for `tarot`.

Two properties matter beyond "it returns the output": that `tarot_strand_seed` and `tarot_check`
write **nothing** (they're the read-only pair — the agent stays the author), and that every
refusal is a plain message rather than a traceback, because that message is all the agent sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session as connect

from panopticon.core.models import Repo, Status
from panopticon.core.tarot import CommandResult, TarotCLI
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.mcp import build_mcp_server
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.tarot_gate import TarotGate
from panopticon.workflows import GithubPeerReviewed

CLONE = "/tasks/t1"
SEED = '{"base_sha": "abc", "head_sha": "def", "strands": []}'

TOOLS = ("tarot_strand_seed", "tarot_check", "tarot_tour_scaffold")


class FakeRun:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        for prefix, result in self._responses.items():
            if argv[: len(prefix)] == prefix:
                return result
        return CommandResult(returncode=0, output="")

    @property
    def tarot_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c and c[0].endswith("tarot")]


async def make_service(
    tmp_path: Path,
    run: FakeRun,
    *,
    opted_in: bool = True,
    binary: str | None = "tarot",
    clone_exists: bool = True,
    clone: str | None = CLONE,
) -> tuple[TaskService, str]:
    gate = TarotGate(
        cli=TarotCLI(run=run, tarot_binary=binary),
        run=run,
        clone_exists=lambda path: clone_exists,
    )
    svc = TaskService(
        SqlAlchemyStore(),
        {"github-peer-reviewed": GithubPeerReviewed()},
        FilesystemArtifactStore(tmp_path),
        tarot_gate=gate,
    )
    await svc.init()
    await svc.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            enabled_workflows=["github-peer-reviewed"],
            capabilities={"tarot_review": True} if opted_in else {},
        )
    )
    task = await svc.create_task("r1", "github-peer-reviewed")
    await svc.set_slug(task.id, "a-task")
    if clone is not None:
        await svc.record_provisioning(task.id, branch="panopticon/a-task", clone=clone)
    return svc, task.id


def text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


# -- the tools are there ----------------------------------------------------------


async def test_the_three_tools_are_registered(tmp_path: Path) -> None:
    svc, _ = await make_service(tmp_path, FakeRun())
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        assert set(TOOLS) <= {t.name for t in (await s.list_tools()).tools}


# -- strand seed ------------------------------------------------------------------


async def test_strand_seed_returns_tarots_enumeration(tmp_path: Path) -> None:
    run = FakeRun({("tarot",): CommandResult(0, SEED)})
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_strand_seed", {"task_id": task_id})

    assert result.isError is False
    assert text_of(result) == SEED
    assert run.tarot_calls == [
        ("tarot", "strands", "suggest", "--json", "--directory", CLONE, "--base", "origin/main")
    ]


async def test_strand_seed_is_read_only(tmp_path: Path) -> None:
    """`--json` is the whole point: no host process may mutate the agent's tree behind its back."""
    run = FakeRun({("tarot",): CommandResult(0, SEED)})
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool("tarot_strand_seed", {"task_id": task_id})

    assert all("--json" in call for call in run.tarot_calls)
    assert not any("scaffold" in call or "install" in call for call in run.tarot_calls)


async def test_strand_seed_surfaces_a_failure_as_a_message(tmp_path: Path) -> None:
    run = FakeRun({("tarot",): CommandResult(2, "tarot: no resolvable base")})
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_strand_seed", {"task_id": task_id})

    assert result.isError is True
    assert "no resolvable base" in text_of(result)


# -- check ------------------------------------------------------------------------


async def test_check_returns_violations_without_touching_the_task(tmp_path: Path) -> None:
    """The tight loop: see the violations without spending a transition to learn them."""
    violation = "src/a.py:f: not claimed by any strand"
    run = FakeRun({("tarot", "strands"): CommandResult(1, violation)})
    svc, task_id = await make_service(tmp_path, run)
    before = await svc.get_task(task_id)

    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_check", {"task_id": task_id})

    assert result.isError is False  # violations are an answer, not a tool failure
    assert violation in text_of(result)
    after = await svc.get_task(task_id)
    assert after.state == before.state
    assert len(after.history) == len(before.history)


async def test_check_reports_success_plainly(tmp_path: Path) -> None:
    svc, task_id = await make_service(tmp_path, FakeRun())
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_check", {"task_id": task_id})

    assert result.isError is False
    assert "valid" in text_of(result)


async def test_check_writes_nothing(tmp_path: Path) -> None:
    run = FakeRun()
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool("tarot_check", {"task_id": task_id})

    assert [c[1:3] for c in run.tarot_calls] == [("strands", "check"), ("tour", "check")]


# -- tour scaffold ----------------------------------------------------------------


async def test_tour_scaffold_writes_and_reports_the_path(tmp_path: Path) -> None:
    run = FakeRun(
        {("tarot",): CommandResult(0, "tarot: wrote tour scaffold to .tarot/tours/x.json")}
    )
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_tour_scaffold", {"task_id": task_id})

    assert result.isError is False
    assert ".tarot/tours/x.json" in text_of(result)
    assert run.tarot_calls[0][:4] == ("tarot", "tour", "scaffold", "--from-strands")


async def test_tour_scaffold_passes_a_title_through(tmp_path: Path) -> None:
    run = FakeRun()
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        await s.call_tool("tarot_tour_scaffold", {"task_id": task_id, "title": "Host-side gate"})

    assert "Host-side gate" in run.tarot_calls[0]


async def test_tour_scaffold_surfaces_a_missing_seed(tmp_path: Path) -> None:
    """Scaffold reads the *edited* seed; without one tarot exits 2 and says so."""
    run = FakeRun({("tarot",): CommandResult(2, "tarot: no seed at .tarot/strands.json")})
    svc, task_id = await make_service(tmp_path, run)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        result = await s.call_tool("tarot_tour_scaffold", {"task_id": task_id})

    assert result.isError is True
    assert "no seed" in text_of(result)


# -- refusals ---------------------------------------------------------------------


async def test_every_tool_refuses_for_a_repo_that_hasnt_opted_in(tmp_path: Path) -> None:
    run = FakeRun()
    svc, task_id = await make_service(tmp_path, run, opted_in=False)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        for tool in TOOLS:
            result = await s.call_tool(tool, {"task_id": task_id})
            assert result.isError is True, tool
            assert "hasn't opted into" in text_of(result), tool
    assert run.calls == []


async def test_every_tool_refuses_when_tarot_isnt_installed(tmp_path: Path) -> None:
    run = FakeRun()
    svc, task_id = await make_service(tmp_path, run, binary=None)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        for tool in TOOLS:
            result = await s.call_tool(tool, {"task_id": task_id})
            assert result.isError is True, tool
            assert "no `tarot` was found" in text_of(result), tool


async def test_every_tool_refuses_when_the_clone_isnt_here(tmp_path: Path) -> None:
    svc, task_id = await make_service(tmp_path, FakeRun(), clone_exists=False)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        for tool in TOOLS:
            result = await s.call_tool(tool, {"task_id": task_id})
            assert result.isError is True, tool
            assert CLONE in text_of(result), tool


async def test_every_tool_refuses_for_an_unprovisioned_task(tmp_path: Path) -> None:
    svc, task_id = await make_service(tmp_path, FakeRun(), clone=None)
    async with connect(build_mcp_server(svc)) as s:
        await s.initialize()
        for tool in TOOLS:
            result = await s.call_tool(tool, {"task_id": task_id})
            assert result.isError is True, tool
            assert "no clone readable" in text_of(result), tool


# -- the served skill -------------------------------------------------------------


async def test_tarots_authoring_skill_is_served_only_for_an_opted_in_repo(tmp_path: Path) -> None:
    body = (
        "---\nname: tarot-authoring\n---\n\n## 1. Suggest a strand seed\n`tarot strands suggest`\n"
    )

    def run(args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        if list(args[1:3]) == ["skill", "install"]:
            skill = Path(args[-1]) / ".claude" / "skills" / "tarot-authoring" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(body)
            return CommandResult(0, "added SKILL.md")
        return CommandResult(0, "")

    svc, task_id = await make_service(tmp_path, run)  # type: ignore[arg-type]
    skills = await svc.skills(task_id)

    tarot_skill = next(s for s in skills if s.name == "tarot-authoring")
    # Tarot's own words, verbatim — panopticon adds only the name mapping, never a paraphrase.
    assert "## 1. Suggest a strand seed" in tarot_skill.instructions
    assert "`tarot strands suggest`" in tarot_skill.instructions
    assert "tarot_strand_seed" in tarot_skill.instructions  # the preamble's mapping
    assert "tarot_tour_scaffold" in tarot_skill.instructions
    assert "name: tarot-authoring" not in tarot_skill.instructions  # no nested frontmatter


async def test_no_tarot_skill_for_a_repo_that_hasnt_opted_in(tmp_path: Path) -> None:
    svc, task_id = await make_service(tmp_path, FakeRun(), opted_in=False)
    assert not any(s.name == "tarot-authoring" for s in await svc.skills(task_id))


async def test_no_tarot_skill_when_tarot_is_absent(tmp_path: Path) -> None:
    """No paraphrase as a consolation prize — the gate's own refusal tells the operator instead."""
    svc, task_id = await make_service(tmp_path, FakeRun(), binary=None)
    names = {s.name for s in await svc.skills(task_id)}
    assert "tarot-authoring" not in names
    assert "provision" in names  # the rest of the skill list is untouched


async def test_the_gate_still_resolves_its_own_responsibility_key(tmp_path: Path) -> None:
    """Sanity: the responsibility the served skill points at is the one the gate writes."""
    from panopticon.taskservice.tarot_gate import RESPONSIBILITY_KEY

    svc, task_id = await make_service(tmp_path, FakeRun())
    for key in ("plan-written", "token-estimated"):
        await svc.resolve_responsibility(task_id, key, status=Status.MET, comment="done")
    await svc.apply_operation(task_id, "advance")

    task = await svc.get_task(task_id)
    assert RESPONSIBILITY_KEY in {r.key for r in task.current_entry.responsibilities}
