"""The Textual dashboard: detail rendering (pure) + a pilot that mounts the app.

Uses a fake client (canned task dicts) so the TUI test is deterministic and offline — the
real HTTP client is covered in test_terminal.py."""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable, Input, Static

from panopticon.terminal import dashboard
from panopticon.terminal.dashboard import Dashboard, _matches, _turn_cell, render_detail

_TASK: dict[str, Any] = {
    "id": "task-abcdef0123",
    "slug": "fix-widget",
    "state": "WORKING",
    "turn": "agent",
    "workflow": "spike",
    "provisioned": True,
    "history": [
        {
            "at": "2026-06-22T10:00:00+00:00",
            "from_state": None, "to_state": "PLAN", "trigger": "start", "responsibilities": [],
        },
        {
            "at": "2026-06-22T11:00:00+00:00",
            "from_state": "PLAN", "to_state": "WORKING", "trigger": "advance",
            "responsibilities": [{"key": "tests-pass", "status": "pending"}],
        },
    ],
}


def _at(stamp: str) -> list[dict[str, Any]]:
    """A one-entry history whose latest timestamp is ``stamp`` (the sort's recency key)."""
    return [{"at": stamp, "from_state": None, "to_state": "WORKING", "responsibilities": []}]


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
        self.created: list[tuple[str, str, str | None]] = []
        self.applied: list[tuple[str, str]] = []
        self.released: list[str] = []

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

    def create_task(
        self, repo_id: str, workflow: str, description: str | None = None
    ) -> dict[str, Any]:
        self.created.append((repo_id, workflow, description))
        return {"id": "new"}

    def apply_operation(self, task_id: str, operation: str) -> dict[str, Any]:
        self.applied.append((task_id, operation))
        return {"id": task_id}

    def release(self, task_id: str) -> dict[str, Any]:
        self.released.append(task_id)
        self._registrations.pop(task_id, None)
        for t in self._tasks:  # reflect the unclaim in list_tasks (as the real service does)
            if t["id"] == task_id:
                t["claimed_by"] = None
        return {"id": task_id, "claimed_by": None}


def test_render_detail_shows_state_turn_and_history() -> None:
    text = render_detail(_TASK)
    assert "fix-widget" in text
    assert "state: WORKING" in text and "turn: agent" in text
    assert "∅ → PLAN (start)" in text
    assert "PLAN → WORKING (advance)" in text
    assert "tests-pass=pending" in text


def test_render_detail_shows_the_description() -> None:
    assert "make the widget green" not in render_detail(_TASK)
    text = render_detail({**_TASK, "description": "make the widget green"})
    assert "make the widget green" in text


def test_render_detail_shows_the_url() -> None:
    assert "url:" not in render_detail(_TASK)
    text = render_detail({**_TASK, "url": "https://github.com/acme/widgets/pull/7"})
    assert "url: https://github.com/acme/widgets/pull/7" in text


def test_render_detail_marks_blocked() -> None:
    assert "(blocked)" not in render_detail(_TASK)
    assert "turn: agent (blocked)" in render_detail({**_TASK, "blocked": True})


def test_turn_cell_color_codes_like_cloude_cade() -> None:
    # cloude-cade: agent=green, user=yellow, blocked=red (blocked wins).
    agent = _turn_cell(_TASK)
    assert agent.plain == "agent" and agent.style == "green"
    user = _turn_cell({**_TASK, "turn": "user"})
    assert user.plain == "user" and user.style == "yellow"
    blocked = _turn_cell({**_TASK, "blocked": True})
    assert blocked.plain == "agent ⚠" and blocked.style == "red"


async def test_dashboard_mounts_lists_tasks_and_shows_detail() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 1
        detail = app.query_one("#detail", Static)
        assert "WORKING" in str(detail.render())


async def test_tasks_are_sorted_live_then_user_then_recent() -> None:
    # The order: (1) non-terminal above terminal, (2) the user's turn above the agent's,
    # (3) most-recently-updated (latest history `at`) first.
    tasks = [
        # terminal tasks — sink below all live work even though their `at` is the most recent.
        {**_TASK, "id": "t-done", "state": "COMPLETE", "turn": "user", "history": _at("2026-06-22T23:00:00+00:00")},
        {**_TASK, "id": "t-drop", "state": "DROPPED", "turn": "agent", "history": _at("2026-06-22T22:00:00+00:00")},
        # live agent-turn tasks — below the user-turn ones, newest of the two first.
        {**_TASK, "id": "t-agent-new", "turn": "agent", "history": _at("2026-06-22T13:00:00+00:00")},
        {**_TASK, "id": "t-agent-old", "turn": "agent", "history": _at("2026-06-22T08:00:00+00:00")},
        # live user-turn tasks — at the very top, newest first.
        {**_TASK, "id": "t-user-new", "turn": "user", "history": _at("2026-06-22T10:00:00+00:00")},
        {**_TASK, "id": "t-user-old", "turn": "user", "history": _at("2026-06-22T09:00:00+00:00")},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        order = [str(k.value) for k in table.rows]
        assert order == [
            "t-user-new", "t-user-old",    # live, user's turn, newest first
            "t-agent-new", "t-agent-old",  # live, agent's turn, newest first
            "t-done", "t-drop",            # terminal last (their recent `at` doesn't lift them)
        ]


async def test_sort_breaks_ties_on_slug() -> None:
    # Same terminal-ness, turn, and `at` → fall back to slug (then id) for a stable order.
    at = _at("2026-06-22T10:00:00+00:00")
    tasks = [
        {**_TASK, "id": "t2", "slug": "zebra", "turn": "user", "history": at},
        {**_TASK, "id": "t1", "slug": "alpha", "turn": "user", "history": at},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert order == ["t1", "t2"]  # alpha < zebra


async def test_dashboard_auto_refreshes_on_the_interval() -> None:
    # A short interval picks up task-list changes without an `r` keypress.
    fake = _FakeClient([])
    app = Dashboard(fake, refresh_interval=0.05)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 0
        fake._tasks = [_TASK]  # the service grew a task; the timer should pick it up
        await pilot.pause(0.15)
        assert table.row_count == 1


async def test_auto_refresh_preserves_the_highlighted_task() -> None:
    # Two tasks; highlight the second, then a refresh must keep the cursor on it (not snap to first).
    other = {**_TASK, "id": "task-second9999", "slug": "other"}
    fake = _FakeClient([_TASK, other])
    app = Dashboard(fake, refresh_interval=0)  # manual refresh only — drive it explicitly
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        assert app._current == "task-second9999"
        app.action_refresh()
        await pilot.pause()
        assert app._current == "task-second9999"  # highlight survived the rebuild
        assert table.cursor_row == 1


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


async def test_pressing_s_switches_to_the_service_session_when_one_exists() -> None:
    # `s` switches to the task-service tmux session via on_service (record + detach, like `t`),
    # and the dashboard stays alive; on_service returns True when a service session exists.
    calls: list[str] = []

    def on_service() -> bool:
        calls.append("service")
        return True

    app = Dashboard(_FakeClient([_TASK]), on_service=on_service)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert calls == ["service"]
        assert app.is_running


async def test_pressing_s_with_no_service_session_does_nothing() -> None:
    app = Dashboard(_FakeClient([_TASK]), on_service=lambda: False)  # no service session
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app.is_running  # reported "none running"; stayed on the dashboard


async def test_pressing_n_creates_a_task_via_repo_workflow_then_description() -> None:
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
        await pilot.press("f", "i", "x")  # type a description into the prompt
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert fake.created == [("r1", "spike", "fix")]


async def test_pressing_n_with_a_blank_description_creates_with_none() -> None:
    fake = _FakeClient([], repos=["r1"], workflows=["spike"])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("enter")  # submit an empty description
        await pilot.pause()
        assert fake.created == [("r1", "spike", None)]


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


async def test_pressing_p_opens_the_task_url(monkeypatch: Any) -> None:
    # `p` opens the highlighted task's url in the browser (cloude-cade's `p` "open PR").
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    task = {**_TASK, "url": "https://github.com/acme/widgets/pull/7"}
    app = Dashboard(_FakeClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert opened == ["https://github.com/acme/widgets/pull/7"]


async def test_pressing_p_with_no_url_does_nothing(monkeypatch: Any) -> None:
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    app = Dashboard(_FakeClient([_TASK]))  # _TASK has no url  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert opened == []  # nothing to open; warned and stayed put
        assert app.is_running


def test_render_detail_shows_the_claim() -> None:
    assert "claimed:" not in render_detail(_TASK)
    assert "claimed: host-1" in render_detail({**_TASK, "claimed_by": "host-1"})


def test_run_status_reflects_claim_liveness_and_provisioning() -> None:
    fake = _FakeClient([], {"t-live": [{"container_id": "c"}], "t-planning": [{"container_id": "c"}]})
    app = Dashboard(fake)  # type: ignore[arg-type]
    live = {"id": "t-live", "claimed_by": "h", "provisioned": True}
    down = {"id": "t-down", "claimed_by": "h", "provisioned": True}
    starting = {"id": "t-start", "claimed_by": "h", "provisioned": False}
    # registered but not provisioned yet — a PLANNING task that hasn't set its slug; it IS live
    planning = {"id": "t-planning", "claimed_by": "h", "provisioned": False}
    assert app._run_status({"id": "t-x"}) == "–"  # unclaimed → not spawned
    assert app._run_status(live) == "live"  # claimed + registered
    assert app._run_status(down) == "down"  # claimed + provisioned, no container
    assert app._run_status(starting) == "starting"  # claimed, no registration yet (booting)
    assert app._run_status(planning) == "live"  # registered → live even though unprovisioned


def test_run_status_shows_respawning_until_reclaimed() -> None:
    fake = _FakeClient([], {"t1": [{"container_id": "c"}]})
    app = Dashboard(fake)  # type: ignore[arg-type]
    app._respawning.add("t1")
    # released by R (unclaimed) → "respawning", not the bare "–" that reads as a lost runner
    assert app._run_status({"id": "t1"}) == "respawning"
    # once the runner re-claims it, the flag clears and the normal down→live boot shows through
    assert app._run_status({"id": "t1", "claimed_by": "h", "provisioned": True}) == "live"
    assert "t1" not in app._respawning
    assert app._run_status({"id": "t1"}) == "–"  # no longer respawning


async def test_respawn_releases_a_down_tasks_claim() -> None:
    task = {**_TASK, "claimed_by": "host-1"}  # claimed but no registration → down
    fake = _FakeClient([task], {})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == [task["id"]]  # released → the runner re-spawns it
        assert task["id"] in app._respawning  # marked respawning (shown instead of bare "–")


def test_matches_is_a_case_insensitive_substring_over_identifying_fields() -> None:
    task = {**_TASK, "slug": "fix-widget", "state": "WORKING", "workflow": "spike"}
    assert _matches(task, "")  # empty query → no filter
    assert _matches(task, "widget")  # slug substring
    assert _matches(task, "WIDGET")  # case-insensitive
    assert _matches(task, "working")  # state
    assert _matches(task, "spike")  # workflow
    assert _matches(task, task["id"][:6])  # id
    assert not _matches(task, "nope")
    # description is searchable too
    assert _matches({**task, "description": "make it green"}, "green")
    assert _matches({**_TASK, "description": None}, "")  # None description doesn't blow up


_FIX = {**_TASK, "id": "t-fix", "slug": "fix-widget", "state": "WORKING", "workflow": "spike"}
_DEP = {**_TASK, "id": "t-dep", "slug": "deploy-api", "state": "PLANNING", "workflow": "github-self-reviewed"}


async def test_pressing_slash_filters_the_task_list_as_you_type() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 2
        await pilot.press("slash")  # enter search mode → the box reveals + focuses
        await pilot.pause()
        assert app.query_one("#search", Input).styles.display == "block"
        await pilot.press("d", "e", "p")  # type a substring of the deploy-api slug
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-dep"]  # only the match remains


async def test_search_matches_state_and_workflow_not_just_slug() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("p", "l", "a", "n")  # matches _DEP's PLANNING state
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-dep"]


async def test_enter_locks_the_filter_and_restores_navigation() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.press("enter")  # lock: box hides, filter stays, table regains focus
        await pilot.pause()
        assert app.query_one("#search", Input).styles.display == "none"
        assert app._query == "fix"  # filter preserved
        assert [str(k.value) for k in table.rows] == ["t-fix"]
        assert table.has_focus  # navigation keys work again


async def test_escape_clears_the_search() -> None:
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.pause()
        assert table.row_count == 1
        await pilot.press("escape")  # clear: query reset, all rows return, box hidden
        await pilot.pause()
        assert app._query == ""
        assert app.query_one("#search", Input).styles.display == "none"
        assert table.row_count == 2


async def test_search_filter_survives_auto_refresh() -> None:
    # The filter lives in action_refresh, so the auto-refresh timer keeps it applied.
    app = Dashboard(_FakeClient([_FIX, _DEP]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        await pilot.press("slash")
        await pilot.press("f", "i", "x")
        await pilot.pause()
        assert table.row_count == 1
        app.action_refresh()  # a rebuild (as the timer would do) keeps the filter
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-fix"]


async def test_respawn_refuses_a_live_task() -> None:
    task = {**_TASK, "claimed_by": "host-1"}
    fake = _FakeClient([task], {task["id"]: [{"container_id": "c"}]})  # live
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == []  # live container → respawn refused (would double-spawn)
