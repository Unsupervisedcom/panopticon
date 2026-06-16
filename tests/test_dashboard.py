"""The Textual dashboard: detail rendering (pure) + a pilot that mounts the app.

Uses a fake client (canned task dicts) so the TUI test is deterministic and offline — the
real HTTP client is covered in test_terminal.py."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable, Static

from panopticon.terminal.dashboard import Dashboard, render_detail

_TASK: dict[str, Any] = {
    "id": "task-abcdef0123",
    "slug": "fix-widget",
    "state": "WORKING",
    "turn": "agent",
    "workflow": "spike",
    "history": [
        {"from_state": None, "to_state": "PLAN", "trigger": "start", "responsibilities": []},
        {
            "from_state": "PLAN", "to_state": "WORKING", "trigger": "advance",
            "responsibilities": [{"key": "tests-pass", "status": "pending"}],
        },
    ],
}


class _FakeClient:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        registrations: dict[str, list[dict[str, Any]]] | None = None,
        *,
        repos: list[str] | None = None,
        workflows: list[str] | None = None,
        operations: dict[str, str] | None = None,
    ) -> None:
        self._tasks = tasks
        self._registrations = registrations or {}
        self._repos = repos or []
        self._workflows = workflows or []
        self._operations = operations or {}
        self.created: list[tuple[str, str]] = []
        self.applied: list[tuple[str, str]] = []

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._tasks

    def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
        return self._registrations.get(task_id, [])

    def list_repos(self) -> list[dict[str, Any]]:
        return [{"id": r} for r in self._repos]

    def list_workflows(self) -> list[str]:
        return self._workflows

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def create_task(self, repo_id: str, workflow: str) -> dict[str, Any]:
        self.created.append((repo_id, workflow))
        return {"id": "new"}

    def apply_operation(self, task_id: str, operation: str) -> dict[str, Any]:
        self.applied.append((task_id, operation))
        return {"id": task_id}


def test_render_detail_shows_state_turn_and_history() -> None:
    text = render_detail(_TASK)
    assert "fix-widget" in text
    assert "state: WORKING" in text and "turn: agent" in text
    assert "∅ → PLAN (start)" in text
    assert "PLAN → WORKING (advance)" in text
    assert "tests-pass=pending" in text


def test_render_detail_marks_blocked() -> None:
    assert "(blocked)" not in render_detail(_TASK)
    assert "turn: agent (blocked)" in render_detail({**_TASK, "blocked": True})


async def test_dashboard_mounts_lists_tasks_and_shows_detail() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 1
        detail = app.query_one("#detail", Static)
        assert "WORKING" in str(detail.render())


async def test_dashboard_with_no_tasks() -> None:
    app = Dashboard(_FakeClient([]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#tasks", DataTable).row_count == 0
        assert str(app.query_one("#detail", Static).render()) == "no tasks"


async def test_pressing_t_signals_the_pick_and_keeps_the_dashboard_running() -> None:
    # The dashboard records the pick via on_switch (the supervisor detaches + attaches the task)
    # and stays alive, so returning lands on this same live dashboard (ADR 0009 §6).
    picked: list[str] = []
    regs = {"task-abcdef0123": [{"container_id": "panopticon-task-abcdef0123"}]}
    app = Dashboard(_FakeClient([_TASK], regs), on_switch=picked.append)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert picked == ["panopticon-task-abcdef0123"]  # session == container id
        assert app.is_running  # did NOT exit — the dashboard session persists


async def test_pressing_t_with_no_running_container_does_not_signal() -> None:
    picked: list[str] = []
    app = Dashboard(_FakeClient([_TASK], {}), on_switch=picked.append)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert picked == []
        assert app.is_running


async def test_pressing_n_creates_a_task_via_repo_then_workflow_picker() -> None:
    fake = _FakeClient([], repos=["r1", "r2"], workflows=["spike"])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # opens the repo picker
        await pilot.pause()
        await pilot.press("enter")  # first repo: r1
        await pilot.pause()
        await pilot.press("enter")  # first (only) workflow: spike
        await pilot.pause()
        assert fake.created == [("r1", "spike")]


async def test_dashboard_drives_drop() -> None:
    # Drop is the one transition the operator drives; advance and the rest are agent skills, so
    # they aren't dashboard actions (no `a`/`i` bindings).
    fake = _FakeClient([_TASK], operations={"advance": "MERGING", "drop": "DROPPED"})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert fake.applied == [("task-abcdef0123", "drop")]
