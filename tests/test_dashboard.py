"""The Textual dashboard: detail rendering (pure) + a pilot that mounts the app.

Uses a fake client (canned task dicts) so the TUI test is deterministic and offline — the
real HTTP client is covered in test_terminal.py."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Checkbox, DataTable, Input, Static

from panopticon.terminal import dashboard
from panopticon.terminal.dashboard import (
    Dashboard,
    _ENSEMBLE_KEY_PREFIX,
    _SEPARATOR_KEY,
    _group_by_governor,
    _group_section,
    _matches,
    _repo_cell,
    _short_tokens,
    _slug_cell,
    _sort_key,
    _status_cell,
    _turn_cell,
    render_detail,
)

_TASK: dict[str, Any] = {
    "id": "task-abcdef0123",
    "repo_id": "default",
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


def _raise(*args: Any, **kwargs: Any) -> Any:
    """Stand in for a failing REST call (e.g. a down service)."""
    raise RuntimeError("service unavailable")


class _FakeClient:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        registrations: dict[str, list[dict[str, Any]]] | None = None,
        *,
        repos: list[str] | list[dict[str, Any]] | None = None,
        workflows: list[dict[str, str]] | None = None,
        operations: dict[str, str] | None = None,
        artifacts: dict[str, list[str]] | None = None,
        artifact_content: bytes = b"",
    ) -> None:
        self._tasks = tasks
        self._registrations = registrations or {}
        # repos may be bare ids (existing task-creation tests) or full dicts (repo-screen tests).
        # Unspecified (None) defaults to one repo present, so the start-up auto-open of the repo
        # screen (fired when there are *no* repos) doesn't pop over tests that don't care; pass an
        # explicit `repos=[]` to exercise the no-repos case.
        if repos is None:
            repos = [{"id": "default", "name": "default", "git_url": "", "default_base": "main"}]
        self._repos: list[dict[str, Any]] = [
            {"id": r, "name": r, "git_url": "", "default_base": "main"} if isinstance(r, str) else r
            for r in repos
        ]
        self._workflows = workflows or []
        self._operations = operations or {}
        self._artifacts = artifacts or {}
        self._artifact_content = artifact_content
        # Change-feed state for the long-poll worker: a version cursor + an event a test arms with
        # `signal_change()` to release a parked `list_tasks_versioned` (the producer "changed a task").
        self._version = 0
        self._change = threading.Event()
        self.list_tasks_calls = 0  # how many times the table was (re)built — counts feed refreshes
        self.created: list[tuple[str, str, str | None]] = []
        self.applied: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.created_repos: list[dict[str, Any]] = []
        self.updated_repos: list[tuple[str, dict[str, Any]]] = []
        self.fetched: list[tuple[str, str]] = []  # (task_id, name) passed to get_artifact

    def list_tasks(self) -> list[dict[str, Any]]:
        self.list_tasks_calls += 1
        return self._tasks

    def list_tasks_versioned(
        self, *, since: int = 0, wait: float | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Mimic the block-until-change feed: park until a test arms `signal_change()` (then bump
        the cursor and return) or the park elapses (return the current, unchanged cursor). `wait=None`
        is the immediate seed read the worker does before its first long-poll.

        The park is **capped** well below a real long-poll's `wait`: the worker thread blocks here,
        and the asyncio default executor is joined at loop teardown, so a multi-second park would
        stall every test's teardown. Capping keeps an idle worker cycling cheaply and teardown
        snappy while still releasing promptly on `signal_change`."""
        timeout = 0.0 if wait is None else min(wait, 0.05)
        if self._change.wait(timeout=timeout):
            self._change.clear()
            self._version += 1
        return self._tasks, self._version

    def signal_change(self) -> None:
        """Release a parked long-poll once, as a task-state change would — the next
        `list_tasks_versioned` returns a bumped cursor and the worker refreshes."""
        self._change.set()

    def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
        return self._registrations.get(task_id, [])

    def list_artifacts(self, task_id: str) -> list[str]:
        return self._artifacts.get(task_id, [])

    def get_artifact(self, task_id: str, name: str) -> bytes:
        self.fetched.append((task_id, name))
        return self._artifact_content

    def list_repos(self) -> list[dict[str, Any]]:
        return self._repos

    def create_repo(
        self, repo_id: str, name: str, git_url: str, default_base: str = "main",
        *, env_file: str | None = None,
        capabilities: dict[str, Any] | None = None,
        enabled_workflows: list[str] | None = None,
        disabled_workflows: list[str] | None = None,
    ) -> dict[str, Any]:
        repo: dict[str, Any] = {
            "id": repo_id, "name": name, "git_url": git_url, "default_base": default_base,
            "env_file": env_file,
            "enabled_workflows": enabled_workflows or [],
            "disabled_workflows": disabled_workflows or [],
        }
        if capabilities is not None:
            repo["capabilities"] = capabilities
        self.created_repos.append(repo)
        self._repos.append(repo)
        return repo

    def update_repo(self, repo_id: str, **changes: Any) -> dict[str, Any]:
        self.updated_repos.append((repo_id, changes))
        for repo in self._repos:
            if repo["id"] == repo_id:
                repo.update(changes)
                return repo
        return {"id": repo_id, **changes}

    def list_workflows(self) -> list[dict[str, str]]:
        return self._workflows

    def list_workflows_for_repo(self, repo_id: str) -> list[dict[str, str]]:
        return self._workflows

    def list_operations(self, task_id: str) -> dict[str, str]:
        return self._operations

    def create_task(
        self, repo_id: str, workflow: str, memo: str | None = None
    ) -> dict[str, Any]:
        self.created.append((repo_id, workflow, memo))
        return {"id": "new"}

    def apply_operation(self, task_id: str, operation: str) -> dict[str, Any]:
        self.applied.append((task_id, operation))
        return {"id": task_id}

    def get_task(self, task_id: str) -> dict[str, Any]:
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        raise KeyError(task_id)

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


def test_render_detail_shows_the_id() -> None:
    assert "id: task-abcdef0123" in render_detail(_TASK)


def test_render_detail_shows_the_memo() -> None:
    assert "make the widget green" not in render_detail(_TASK)
    text = render_detail({**_TASK, "memo": "make the widget green"})
    assert "make the widget green" in text


def test_render_detail_shows_the_url() -> None:
    assert "url:" not in render_detail(_TASK)
    text = render_detail({**_TASK, "url": "https://github.com/acme/widgets/pull/7"})
    assert "url: https://github.com/acme/widgets/pull/7" in text


def test_render_detail_is_plain_text_with_brackets_literal() -> None:
    # The detail must be plain (the caller wraps it in Text and renders literally) — never markup.
    task = {**_TASK, "memo": "do [the thing]",
            "lifecycle_detail": "docker run ['--add-host', '--privileged']"}
    out = render_detail({**task, "container_status": "failed"})
    assert "['--add-host', '--privileged']" in out and "[the thing]" in out  # brackets kept verbatim


async def test_dashboard_detail_survives_a_bracketed_lifecycle_detail() -> None:
    # Regression: a docker-command lifecycle_detail (a Python list repr with "[") rendered through
    # Textual's markup parser raised MarkupError and crashed the *whole dashboard* on startup, so
    # `make start` looked broken. The detail pane must render it literally (wrapped in Text).
    task = {
        **_TASK,
        "container_status": "failed",
        "lifecycle_detail": "Command ['docker','run','--add-host','--privileged'] returned 1",
    }
    app = Dashboard(_FakeClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:  # would raise here if the detail crashed the app
        await pilot.pause()
        await pilot.press("d")  # open the detail pane to trigger the fetch
        await pilot.pause()
        assert "--add-host" in str(app.query_one("#detail", Static).render())  # rendered, didn't crash


def test_render_detail_shows_the_tokens_used() -> None:
    assert "tokens:" not in render_detail(_TASK)  # both absent → no line
    assert "tokens: 1.2K used / - est" in render_detail({**_TASK, "tokens_used": 1234})
    # the estimate alone (no usage yet) still renders the line
    assert "tokens: - used / 500.0K est" in render_detail({**_TASK, "token_estimate": 500000})


def test_render_detail_marks_blocked() -> None:
    assert "(blocked)" not in render_detail(_TASK)
    assert "turn: agent (blocked)" in render_detail({**_TASK, "blocked": True})


def test_short_tokens_formats_human_short() -> None:
    assert _short_tokens(None) == "-"  # not yet reported
    assert _short_tokens(0) == "-"
    assert _short_tokens(300) == "300"  # under 1000 verbatim
    assert _short_tokens(1234) == "1.2K"
    assert _short_tokens(1_100_000) == "1.1M"
    assert _short_tokens(2_500_000_000) == "2.5B"


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
        # detail pane is hidden by default — open it, then check content
        await pilot.press("d")
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert "WORKING" in str(detail.render())


async def test_detail_pane_is_hidden_by_default() -> None:
    # the detail pane starts hidden so the task table gets the full width; `d` reveals it.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert not app._detail_visible and detail.styles.display == "none"


async def test_pressing_d_toggles_the_detail_pane() -> None:
    # `d` reveals the (hidden-by-default) detail pane and hides it again.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.query_one("#detail", Static)
        assert not app._detail_visible and detail.styles.display == "none"
        await pilot.press("d")  # show
        await pilot.pause()
        assert app._detail_visible and detail.styles.display == "block"
        await pilot.press("d")  # hide again
        await pilot.pause()
        assert not app._detail_visible and detail.styles.display == "none"


async def test_tasks_are_sorted_live_then_user_then_slug() -> None:
    # The order: (1) non-terminal above terminal, (2) turn priority differs by group — active tasks
    # surface the user's turn first (operator action needed), terminal tasks surface the agent's turn
    # first (just finished); (3) most recently updated first, (4) slug (then id) as the stable tiebreaker.
    # Here all tasks share the same updated_at (None → 0.0), so slug is the effective tiebreaker.
    tasks = [
        # terminal tasks — sink below all live work; agent-turn rises to top of this section.
        {**_TASK, "id": "t-done", "slug": "done", "state": "COMPLETE", "turn": "user"},
        {**_TASK, "id": "t-drop", "slug": "drop", "state": "DROPPED", "turn": "agent"},
        # live agent-turn tasks — below the user-turn ones, sorted by slug.
        {**_TASK, "id": "t-agent-b", "slug": "bravo", "turn": "agent"},
        {**_TASK, "id": "t-agent-a", "slug": "alpha", "turn": "agent"},
        # live user-turn tasks — at the very top, sorted by slug.
        {**_TASK, "id": "t-user-b", "slug": "zebra", "turn": "user"},
        {**_TASK, "id": "t-user-a", "slug": "mango", "turn": "user"},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        keys = [str(k.value) for k in table.rows]
        order = [k for k in keys if k != _SEPARATOR_KEY]  # task order, ignoring the divider row
        assert order == [
            "t-user-a", "t-user-b",    # live, user's turn, slug order
            "t-agent-a", "t-agent-b",  # live, agent's turn, slug order
            "t-drop", "t-done",        # terminal: agent-turn first (just finished), then user-turn
        ]
        # the divider sits exactly between the last active row and the first terminal one
        assert keys.index(_SEPARATOR_KEY) == keys.index("t-agent-b") + 1
        assert keys.index(_SEPARATOR_KEY) == keys.index("t-drop") - 1


async def test_sort_uses_recency_within_tier() -> None:
    # Within the same (terminal, turn) tier, more recently updated tasks sort first.
    tasks = [
        {**_TASK, "id": "t-old", "slug": "alpha", "turn": "user", "updated_at": "2026-06-01T00:00:00"},
        {**_TASK, "id": "t-new", "slug": "zebra", "turn": "user", "updated_at": "2026-06-25T00:00:00"},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert order == ["t-new", "t-old"]  # newer first, despite "zebra" > "alpha"


async def test_sort_breaks_ties_on_slug() -> None:
    # Same terminal-ness, turn, and updated_at → fall back to slug (then id) for a stable order.
    tasks = [
        {**_TASK, "id": "t2", "slug": "zebra", "turn": "user"},
        {**_TASK, "id": "t1", "slug": "alpha", "turn": "user"},
    ]
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        order = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert order == ["t1", "t2"]  # alpha < zebra


# -- active/terminal divider --------------------------------------------------------

_ACTIVE_A = {**_TASK, "id": "t-a", "slug": "alpha", "state": "WORKING", "turn": "user"}
_ACTIVE_B = {**_TASK, "id": "t-b", "slug": "bravo", "state": "ITERATING", "turn": "user"}
_TERM_A = {**_TASK, "id": "t-done", "slug": "done", "state": "COMPLETE", "turn": "user"}
_TERM_B = {**_TASK, "id": "t-drop", "slug": "dropped", "state": "DROPPED", "turn": "user"}


async def test_separator_divides_active_from_terminal() -> None:
    # With both groups present, a single divider row splices in at the boundary.
    app = Dashboard(_FakeClient([_ACTIVE_A, _TERM_A, _ACTIVE_B, _TERM_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        keys = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert keys == ["t-a", "t-b", _SEPARATOR_KEY, "t-done", "t-drop"]


async def test_no_separator_when_all_active() -> None:
    app = Dashboard(_FakeClient([_ACTIVE_A, _ACTIVE_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        keys = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert _SEPARATOR_KEY not in keys


async def test_no_separator_when_all_terminal() -> None:
    app = Dashboard(_FakeClient([_TERM_A, _TERM_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        keys = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert _SEPARATOR_KEY not in keys


async def test_no_separator_when_filtered_to_one_group() -> None:
    # A search filter that leaves only active tasks shows no divider.
    app = Dashboard(_FakeClient([_ACTIVE_A, _ACTIVE_B, _TERM_A, _TERM_B]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.press("a", "l", "p", "h", "a")  # matches only _ACTIVE_A's slug
        await pilot.pause()
        keys = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        assert keys == ["t-a"]  # only the match; no divider


async def test_highlighting_the_separator_selects_no_task() -> None:
    # If the cursor is forced onto the divider, it selects nothing and actions no-op.
    fake = _FakeClient([_ACTIVE_A, _TERM_A])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        sep_index = [str(k.value) for k in table.rows].index(_SEPARATOR_KEY)
        app._update_detail(_SEPARATOR_KEY)  # as a raw highlight on the sentinel would
        await pilot.pause()
        assert app._current is None  # no task selected
        assert str(app.query_one("#detail", Static).render()) == ""  # blank pane
        await pilot.press("x")  # drop no-ops with no current task
        await pilot.pause()
        assert fake.applied == []
        assert sep_index == 1  # divider after the one active row


async def test_arrow_keys_skip_the_separator() -> None:
    # Arrowing across the boundary jumps the divider in both directions.
    app = Dashboard(_FakeClient([_ACTIVE_A, _TERM_A]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=0)  # the active task
        await pilot.pause()
        assert app._current == "t-a"
        await pilot.press("down")  # would land on the divider → skip to the terminal task
        await pilot.pause()
        assert app._current == "t-done"
        await pilot.press("up")  # back up over the divider → the active task again
        await pilot.pause()
        assert app._current == "t-a"


async def _settle(pilot: Any, predicate: Any, *, tries: int = 100, step: float = 0.02) -> None:
    """Pump the event loop until ``predicate()`` holds (or we run out of tries). The feed worker
    runs on a thread and marshals the rebuild back via ``call_from_thread``, so we poll rather than
    sleep a fixed span — robust against scheduling jitter in CI."""
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(step)


async def test_dashboard_refreshes_when_the_feed_signals_a_change() -> None:
    # No wall-clock timer: the long-poll worker redraws the table when the change feed reports a
    # task changed — exactly once per change, and the rebuild reflects the new snapshot.
    fake = _FakeClient([])
    app = Dashboard(fake, refresh_interval=0.05)  # short long-poll wait so idle polls cycle fast  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 0
        builds = fake.list_tasks_calls  # the first paint
        fake._tasks = [_TASK]  # the producer grew a task...
        fake.signal_change()  # ...and the feed releases the worker's parked long-poll
        await _settle(pilot, lambda: table.row_count == 1)
        assert table.row_count == 1
        assert fake.list_tasks_calls == builds + 1  # exactly one feed-driven rebuild


async def test_dashboard_does_not_refresh_while_the_feed_is_idle() -> None:
    # A quiet feed (no change signalled) drives no rebuild, however many long-poll cycles elapse —
    # the old fixed-interval timer would have redrawn regardless.
    fake = _FakeClient([_TASK])
    app = Dashboard(fake, refresh_interval=0.02)  # fast idle polls, but nothing changes  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        builds = fake.list_tasks_calls  # first paint only
        await pilot.pause(0.2)  # several idle long-poll cycles elapse
        assert fake.list_tasks_calls == builds  # the quiet feed triggered no rebuild


async def test_auto_refresh_preserves_the_highlighted_task() -> None:
    # Two tasks; highlight the second, then a refresh must keep the cursor on it (not snap to first).
    other = {**_TASK, "id": "task-second9999", "slug": "other"}
    fake = _FakeClient([_TASK, other])
    app = Dashboard(fake, refresh_interval=0)  # feed worker disabled — drive the rebuild explicitly
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
        await pilot.press("d")  # open the detail pane
        await pilot.pause()
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


async def test_pressing_u_switches_to_the_runner_session_when_one_exists() -> None:
    # `u` switches to the session-service (runner) tmux session via on_runner (record + detach,
    # like `s`), and the dashboard stays alive; on_runner returns True when a runner session exists.
    calls: list[str] = []

    def on_runner() -> bool:
        calls.append("runner")
        return True

    app = Dashboard(_FakeClient([_TASK]), on_runner=on_runner)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert calls == ["runner"]
        assert app.is_running


async def test_pressing_u_with_no_runner_session_does_nothing() -> None:
    app = Dashboard(_FakeClient([_TASK]), on_runner=lambda: False)  # no runner session
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert app.is_running  # reported "none running"; stayed on the dashboard


async def test_pressing_n_creates_a_task_via_repo_workflow_then_memo() -> None:
    fake = _FakeClient([], repos=["r1", "r2"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # opens the repo picker
        await pilot.pause()
        await pilot.press("enter")  # first repo: r1
        await pilot.pause()
        await pilot.press("enter")  # first (only) workflow: spike
        await pilot.pause()
        await pilot.press("f", "i", "x")  # type a memo into the prompt
        await pilot.press("enter")  # submit
        await pilot.pause()
        assert fake.created == [("r1", "spike", "fix")]


async def test_pressing_n_with_a_blank_memo_creates_with_none() -> None:
    fake = _FakeClient([], repos=["r1"], workflows=[{"name": "spike", "when_to_use": ""}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")  # repo
        await pilot.pause()
        await pilot.press("enter")  # workflow
        await pilot.pause()
        await pilot.press("enter")  # submit an empty memo
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


def test_clipboard_command_is_platform_appropriate(monkeypatch: Any) -> None:
    # The result is cached (the installed tool can't change at runtime); clear it before each
    # probe so the monkeypatched platform/PATH takes effect, and once more at the end so the
    # cache doesn't leak the fakes into other tests.
    dashboard._clipboard_command.cache_clear()
    # macOS → pbcopy unconditionally (always present, no `which` probe).
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    assert dashboard._clipboard_command() == ["pbcopy"]
    # Linux → the first installed of wl-copy / xclip / xsel.
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.sys, "platform", "linux")
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: name == "xclip")
    assert dashboard._clipboard_command() == ["xclip", "-selection", "clipboard"]
    # Wayland wins when both are present (preference order).
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: name in {"wl-copy", "xclip"})
    assert dashboard._clipboard_command() == ["wl-copy"]
    # nothing installed → None (no host tool; the OSC 52 emit still applies separately).
    dashboard._clipboard_command.cache_clear()
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: None)
    assert dashboard._clipboard_command() is None
    dashboard._clipboard_command.cache_clear()


async def test_pressing_y_copies_the_slug(monkeypatch: Any) -> None:
    # `y` copies the highlighted task's slug to the host clipboard tool.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert copied == ["fix-widget"]


async def test_pressing_y_with_no_slug_warns(monkeypatch: Any) -> None:
    # An unprovisioned task (no slug) → nothing copied; warn and stay up.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([{**_TASK, "slug": None}]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert copied == []
        assert app.is_running


async def test_pressing_shift_y_copies_the_id(monkeypatch: Any) -> None:
    # `Y` copies the highlighted task's internal id.
    copied: list[str] = []
    monkeypatch.setattr(dashboard, "_clipboard_copy", lambda text: bool(copied.append(text)))
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("Y")
        await pilot.pause()
        assert copied == ["task-abcdef0123"]


def test_render_detail_shows_the_claim() -> None:
    assert "claimed:" not in render_detail(_TASK)
    assert "claimed: host-1" in render_detail({**_TASK, "claimed_by": "host-1"})


def test_status_cell_displays_the_composed_status_color_coded() -> None:
    # The dashboard no longer computes status — the task service composes `container_status` and the
    # cell just renders it (color-coded). No per-task registration calls anymore.
    assert _status_cell({"container_status": "live"}).plain == "live"
    assert _status_cell({"container_status": "live"}).style == "green"
    assert _status_cell({"container_status": "building"}).style == "yellow"  # spawn in flight
    assert _status_cell({"container_status": "healing"}).plain == "healing"
    assert _status_cell({"container_status": "healing"}).style == "cyan"  # self-heal in progress
    assert _status_cell({"container_status": "down"}).style == "red"  # needs attention
    assert _status_cell({"container_status": "failed"}).style == "red"
    assert _status_cell({"container_status": "disconnected"}).style == "red"
    assert _status_cell({"container_status": "–"}).plain == "–"  # terminal task
    assert _status_cell({}).plain == "–"  # missing → em-dash, no crash


async def test_status_cell_is_used_without_per_task_registration_calls() -> None:
    # Building the table must not fan out a registrations request per row (the old N+1) — the status
    # rides on each task dict from the single list_tasks. A registrations call here would raise.
    class _NoRegClient(_FakeClient):
        def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
            raise AssertionError("the table must not call list_registrations per task")

    task = {**_TASK, "container_status": "live"}
    app = Dashboard(_NoRegClient([task]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:  # mounts + paints the table via action_refresh
        await pilot.pause()


async def test_respawn_releases_a_down_tasks_claim() -> None:
    task = {**_TASK, "claimed_by": "host-1", "container_status": "down"}
    fake = _FakeClient([task], {})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == [task["id"]]  # released → the runner re-spawns it


def test_matches_is_a_case_insensitive_substring_over_identifying_fields() -> None:
    task = {**_TASK, "slug": "fix-widget", "state": "WORKING", "workflow": "spike"}
    assert _matches(task, "")  # empty query → no filter
    assert _matches(task, "widget")  # slug substring
    assert _matches(task, "WIDGET")  # case-insensitive
    assert _matches(task, "working")  # state
    assert _matches(task, "spike")  # workflow
    assert not _matches(task, task["id"][:6])  # id is not a search field
    assert not _matches(task, "nope")
    # memo is searchable too
    assert _matches({**task, "memo": "make it green"}, "green")
    assert _matches({**_TASK, "memo": None}, "")  # None memo doesn't blow up


def test_repo_cell_returns_name_from_cache() -> None:
    names = {"r1": "acme/widgets", "r2": "acme/api"}
    assert _repo_cell({"repo_id": "r1"}, names) == "acme/widgets"
    assert _repo_cell({"repo_id": "r2"}, names) == "acme/api"
    assert _repo_cell({"repo_id": "unknown"}, names) == "unknown"  # fallback: bare id
    assert _repo_cell({}, names) == "?"  # no repo_id at all


def test_slug_cell_combines_slug_and_memo() -> None:
    # Returns a Rich Text (not a markup str) so the "[" survives — compare on .plain.
    # both present → slug[memo]
    assert _slug_cell({**_TASK, "slug": "fix-widget", "memo": "make it green"}).plain == (
        "fix-widget[make it green]"
    )
    # slug, no memo → bare slug
    assert _slug_cell({**_TASK, "slug": "fix-widget", "memo": None}).plain == "fix-widget"
    assert _slug_cell({"slug": "fix-widget"}).plain == "fix-widget"  # memo key absent
    # no slug, with memo → "[memo]" (no leading dash)
    assert _slug_cell({"slug": None, "memo": "make it green"}).plain == "[make it green]"
    assert _slug_cell({"memo": "make it green"}).plain == "[make it green]"  # slug key absent
    # neither → "-"
    assert _slug_cell({"slug": None}).plain == "-"
    assert _slug_cell({}).plain == "-"


def test_slug_cell_is_text_so_brackets_arent_eaten_as_markup() -> None:
    # The regression: a bare string cell is rendered through Textual markup, which swallows "[…]"
    # (e.g. "fix-widget[make it green]" → "fix-widget"). A Text renders literally.
    from rich.text import Text

    cell = _slug_cell({"slug": "fix-widget", "memo": "make it green"})
    assert isinstance(cell, Text)
    assert Text.from_markup(cell.plain).plain != cell.plain  # plain str WOULD be mangled by markup


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
    # The filter lives in action_refresh, so a change-feed rebuild keeps it applied.
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


async def test_repo_column_shows_repo_name() -> None:
    # The "repo" column displays the repo's human-readable name, not its id.
    repos = [{"id": "r1", "name": "acme/widgets", "git_url": "", "default_base": "main"}]
    task = {**_TASK, "repo_id": "r1"}
    app = Dashboard(_FakeClient([task], repos=repos))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        row = table.get_row(task["id"])
        # row is (state, turn, container, repo, slug[memo]) — repo is index 3
        assert row[3] == "acme/widgets"


async def test_search_matches_repo_name() -> None:
    # Filtering by repo name surfaces only tasks belonging to that repo.
    repos = [
        {"id": "r1", "name": "acme/widgets", "git_url": "", "default_base": "main"},
        {"id": "r2", "name": "acme/api", "git_url": "", "default_base": "main"},
    ]
    task_w = {**_TASK, "id": "t-w", "repo_id": "r1", "slug": "fix-widget"}
    task_a = {**_TASK, "id": "t-a", "repo_id": "r2", "slug": "add-endpoint"}
    app = Dashboard(_FakeClient([task_w, task_a], repos=repos))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.row_count == 2
        await pilot.press("slash")
        await pilot.press("w", "i", "d", "g", "e", "t", "s")  # matches "acme/widgets" repo name
        await pilot.pause()
        assert [str(k.value) for k in table.rows] == ["t-w"]


async def test_repo_names_refresh_after_repo_edit() -> None:
    # After the repo-config screen closes, the column reflects the updated name.
    repos = [{"id": "r1", "name": "old-name", "git_url": "", "default_base": "main"}]
    task = {**_TASK, "repo_id": "r1"}
    fake = _FakeClient([task], repos=repos)
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        assert table.get_row(task["id"])[3] == "old-name"
        # Simulate a rename via the service (as RepoFormScreen would do) then the dismiss reload.
        fake.update_repo("r1", name="new-name")
        app._load_repo_names()
        app.action_refresh()
        await pilot.pause()
        assert table.get_row(task["id"])[3] == "new-name"


async def test_respawn_releases_a_live_tasks_claim() -> None:
    task = {**_TASK, "claimed_by": "host-1", "container_status": "live"}
    fake = _FakeClient([task], {task["id"]: [{"container_id": "c"}]})
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert fake.released == [task["id"]]  # live container → released so runner kills + respawns


# -- repo config screen (`g`) -------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/widgets.git", "widgets"),
        ("git@github.com:acme/widgets.git", "widgets"),
        ("https://github.com/acme/widgets", "widgets"),  # no .git suffix
        ("https://github.com/acme/widgets/", "widgets"),  # trailing slash
        ("ssh://git@host:22/acme/widgets.git", "widgets"),
        ("", ""),  # empty
        ("widgets", ""),  # bare token, no path → unparseable
        ("   ", ""),  # whitespace only
    ],
)
def test_repo_name_from_git_url(url: str, expected: str) -> None:
    assert dashboard._repo_name_from_git_url(url) == expected


async def test_pressing_g_opens_the_repos_screen_listing_repos() -> None:
    fake = _FakeClient([], repos=[{"id": "r1", "name": "acme/widgets", "git_url": "https://x/r1.git",
                                   "default_base": "main"}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.ReposScreen)
        table = app.screen.query_one("#repos", DataTable)
        assert table.row_count == 1


async def test_no_repos_auto_opens_the_repos_screen_on_start() -> None:
    # First-run nudge: with no repos configured, the dashboard drops straight into the repo
    # screen so the operator can add one (a task can't be created without a repo).
    app = Dashboard(_FakeClient([_TASK], repos=[]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, dashboard.ReposScreen)


async def test_repos_present_does_not_auto_open_the_repos_screen() -> None:
    # The common case: at least one repo → no auto-open, the operator lands on the task view.
    app = Dashboard(_FakeClient([_TASK], repos=["r1"]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.ReposScreen)


async def test_repo_fetch_error_does_not_auto_open_the_repos_screen() -> None:
    # A down service can't list repos (and the repo screen couldn't either) — treat repos as
    # present and leave the operator on the task view rather than popping a screen that'd fail.
    fake = _FakeClient([_TASK], repos=[])
    fake.list_repos = _raise  # type: ignore[method-assign]
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, dashboard.ReposScreen)


async def test_repos_screen_creates_a_repo_autofilling_from_the_git_url() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")  # open the create form
        await pilot.pause()
        # Only the git URL is typed; id and name auto-fill from it, default_base defaults to main.
        app.screen.query_one("#field-git_url", Input).value = "git@github.com:acme/widgets.git"
        await pilot.press("enter")  # submit the form
        await pilot.pause()
        assert fake.created_repos == [
            {"id": "widgets", "name": "widgets", "git_url": "git@github.com:acme/widgets.git",
             "default_base": "main", "env_file": None,
             "enabled_workflows": [], "disabled_workflows": [],
             "capabilities": {"docker_in_docker": False}}
        ]


async def test_repo_form_autofill_only_fills_blank_fields() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-id", Input).value = "r9"  # pre-typed → kept
        app.screen.query_one("#field-name", Input).value = "acme/new"  # pre-typed → kept
        await pilot.press("enter")
        await pilot.pause()
        # id/name keep the user's values — pre-typed fields are never clobbered by autofill.
        assert fake.created_repos == [
            {"id": "r9", "name": "acme/new", "git_url": "https://x/widgets.git",
             "default_base": "main", "env_file": None,
             "enabled_workflows": [], "disabled_workflows": [],
             "capabilities": {"docker_in_docker": False}}
        ]


async def test_repo_form_git_url_leads_and_default_base_prefills_main() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        # git_url is the first Input on the form; default_base is pre-filled with main.
        inputs = [w.id for w in app.screen.query(Input)]
        assert inputs[0] == "field-git_url"
        assert inputs.index("field-git_url") < inputs.index("field-id") < inputs.index("field-name")
        assert app.screen.query_one("#field-default_base", Input).value == "main"


async def test_repo_form_autofills_on_git_url_blur() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-name", Input).focus()  # blur git_url
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == "widgets"
        assert app.screen.query_one("#field-id", Input).value == "widgets"


async def test_repo_form_edit_mode_does_not_autofill_blank_fields() -> None:
    fake = _FakeClient([], repos=[{"id": "r1", "name": "", "git_url": "https://x/widgets.git",
                                   "default_base": "main"}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit
        await pilot.pause()
        # Editing an existing repo never derives values: blanks stay blank even on blur.
        app.screen.query_one("#field-env_file", Input).focus()  # blur git_url
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == ""
        assert app.screen.query_one("#field-env_file", Input).value == ""


async def test_repos_screen_create_requires_id_name_and_git_url() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-id", Input).value = "r9"  # name + git_url left blank
        await pilot.press("enter")
        await pilot.pause()
        assert fake.created_repos == []  # refused; nothing created


async def test_repos_screen_edits_a_repo_via_patch() -> None:
    fake = _FakeClient([], repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git",
                                   "default_base": "main"}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        assert app.screen.query_one("#field-name", Input).value == "old"  # pre-populated
        app.screen.query_one("#field-name", Input).value = "new"
        await pilot.press("enter")
        await pilot.pause()
        # Core fields plus the privileged capability are sent; image_layer_file is untouched (PATCH).
        # The checkbox is unchecked here, so docker_in_docker is False.
        assert fake.updated_repos == [
            ("r1", {"name": "new", "git_url": "https://x/r1.git", "default_base": "main",
                    "env_file": None,
                    "capabilities": {"docker_in_docker": False},
                    "enabled_workflows": [], "disabled_workflows": []})
        ]


async def test_repo_form_workflow_fields_are_saved_and_reloaded() -> None:
    existing = {"id": "r1", "name": "old", "git_url": "https://x/r1.git", "default_base": "main",
                "enabled_workflows": ["github-self-reviewed"],
                "disabled_workflows": ["orchestrator"]}
    fake = _FakeClient([], repos=[existing])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        # Pre-populated from the stored repo
        assert app.screen.query_one("#field-enabled_workflows", Input).value == "github-self-reviewed"
        assert app.screen.query_one("#field-disabled_workflows", Input).value == "orchestrator"
        # User changes enabled workflows
        app.screen.query_one("#field-enabled_workflows", Input).value = "github-self-reviewed, github-peer-reviewed"
        app.screen.query_one("#field-disabled_workflows", Input).value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert fake.updated_repos[0][1]["enabled_workflows"] == ["github-self-reviewed", "github-peer-reviewed"]
        assert fake.updated_repos[0][1]["disabled_workflows"] == []


async def test_repos_screen_creates_a_repo_with_privileged_docker_enabled() -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-docker_in_docker", Checkbox).value = True  # toggle privileged on
        await pilot.press("enter")
        await pilot.pause()
        # The toggle maps to capabilities.docker_in_docker, which drives the runner's --privileged.
        assert fake.created_repos[0]["capabilities"] == {"docker_in_docker": True}


async def test_repos_screen_edit_toggles_privileged_on_merging_existing_capabilities() -> None:
    fake = _FakeClient([], repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git",
                                   "default_base": "main",
                                   "capabilities": {"some_other_cap": True}}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        app.screen.query_one("#field-docker_in_docker", Checkbox).value = True
        await pilot.press("enter")
        await pilot.pause()
        # docker_in_docker is set; the pre-existing unrelated capability is preserved (merged).
        repo_id, changes = fake.updated_repos[0]
        assert repo_id == "r1"
        assert changes["capabilities"] == {"some_other_cap": True, "docker_in_docker": True}


async def test_repo_form_prechecks_the_toggle_for_a_privileged_repo() -> None:
    fake = _FakeClient([], repos=[{"id": "r1", "name": "old", "git_url": "https://x/r1.git",
                                   "default_base": "main",
                                   "capabilities": {"docker_in_docker": True}}])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("e")  # edit the highlighted repo
        await pilot.pause()
        # A repo already opted into privileged docker opens the form with the box checked.
        assert app.screen.query_one("#field-docker_in_docker", Checkbox).value is True


async def test_repo_form_enter_saves_even_while_the_checkbox_is_focused() -> None:
    # Enter saves from any field, including the privileged-docker checkbox (which toggles on
    # Space only, so Enter bubbles up to the screen's submit binding).
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#field-git_url", Input).value = "https://x/widgets.git"
        app.screen.query_one("#field-docker_in_docker", Checkbox).focus()
        await pilot.pause()
        await pilot.press("enter")  # saves rather than toggling the checkbox
        await pilot.pause()
        assert len(fake.created_repos) == 1
        assert fake.created_repos[0]["id"] == "widgets"
        # Enter didn't toggle the box on its way out.
        assert fake.created_repos[0]["capabilities"] == {"docker_in_docker": False}


async def test_repo_form_space_toggles_the_checkbox_without_saving() -> None:
    # Space toggles the focused checkbox and does not submit the form.
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        checkbox = app.screen.query_one("#field-docker_in_docker", Checkbox)
        assert checkbox.value is False
        checkbox.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert checkbox.value is True  # toggled
        assert fake.created_repos == []  # but not saved
        assert isinstance(app.screen, dashboard.RepoFormScreen)  # form still open


def _record_popen(monkeypatch: Any) -> list[list[str]]:
    """Capture `subprocess.Popen` argv (the host-open call) without launching anything."""
    calls: list[list[str]] = []
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda argv, *a, **k: calls.append(list(argv)))
    return calls


def test_open_command_is_xdg_open_on_linux_and_open_on_mac(monkeypatch: Any) -> None:
    monkeypatch.setattr(dashboard.sys, "platform", "linux")
    assert dashboard._open_command() == "xdg-open"
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    assert dashboard._open_command() == "open"


async def test_pressing_a_opens_the_selected_artifact_via_rest(monkeypatch: Any) -> None:
    # `a` lists the task's artifacts; Enter fetches the selection over REST to a temp file and
    # opens it with the host handler — the universal path (works even remote from the store).
    calls = _record_popen(monkeypatch)
    fake = _FakeClient(
        [_TASK], artifacts={_TASK["id"]: ["plan.md", "notes.md"]}, artifact_content=b"# Plan\n"
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")  # open the artifacts modal
        await pilot.pause()
        await pilot.press("enter")  # select the first artifact → REST open
        await pilot.pause()
        assert fake.fetched == [(_TASK["id"], "plan.md")]
        assert len(calls) == 1
        opener, path = calls[0]
        assert opener == dashboard._open_command()
        assert Path(path).name == "plan.md"  # basename (extension) preserved for the handler
        assert Path(path).read_bytes() == b"# Plan\n"


async def test_rest_open_reuses_one_scratch_dir_and_cleans_it_up(monkeypatch: Any) -> None:
    # Opening several artifacts reuses a single temp dir (no per-open leak), removed on exit.
    calls = _record_popen(monkeypatch)
    fake = _FakeClient(
        [_TASK], artifacts={_TASK["id"]: ["plan.md", "notes.md"]}, artifact_content=b"x"
    )
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):  # open two different artifacts
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        roots = {str(Path(path).parent.parent) for _, path in calls}
        assert len(roots) == 1  # both opens landed under the same scratch root
        scratch = next(iter(roots))
        assert app._artifact_tmp is not None and Path(scratch).is_dir()
    assert not Path(scratch).exists()  # cleaned up on unmount


async def test_pressing_e_opens_a_locally_present_artifact_in_place(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # `e` opens the on-disk artifact directly (no temp copy, no REST) when the dashboard shares
    # the store's filesystem — resolved through FilesystemArtifactStore, which owns the layout.
    calls = _record_popen(monkeypatch)
    art = tmp_path / "tasks" / str(_TASK["id"]) / "plan.md"
    art.parent.mkdir(parents=True)
    art.write_text("# Local\n")
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]}, artifact_content=b"REST")
    app = Dashboard(fake, artifacts_root=tmp_path)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == [[dashboard._open_command(), str(art)]]  # the real file, in place
        assert fake.fetched == []  # no REST fetch — opened the local file


async def test_e_warns_when_the_artifact_is_not_local(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # No co-located file → warn and do nothing (no silent REST fallback).
    calls = _record_popen(monkeypatch)
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]})
    app = Dashboard(fake, artifacts_root=tmp_path)  # empty root  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == []  # nothing opened
        assert fake.fetched == []  # and no REST fallback


async def test_missing_opener_binary_is_handled_not_crashed(monkeypatch: Any) -> None:
    # On a headless host without `xdg-open`, Popen raises FileNotFoundError; the dashboard must
    # notify and stay up rather than let it escape the screen callback and kill the TUI.
    def _raise(argv: Any, *a: Any, **k: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(dashboard.subprocess, "Popen", _raise)
    fake = _FakeClient([_TASK], artifacts={_TASK["id"]: ["plan.md"]}, artifact_content=b"x")
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")  # REST open → Popen raises FileNotFoundError
        await pilot.pause()
        assert app.is_running  # handled, TUI survived


async def test_pressing_a_with_no_artifacts_warns_and_opens_no_modal(monkeypatch: Any) -> None:
    calls = _record_popen(monkeypatch)
    fake = _FakeClient([_TASK], artifacts={})  # task has no artifacts
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert calls == []
        assert len(app.screen_stack) == 1  # the modal was not pushed
        assert app.is_running


# -- help screen (`?`) --------------------------------------------------------------


def test_footer_shows_only_the_essential_keys() -> None:
    # The legend keeps the few most-used keys; the rest still dispatch but are hidden (show=False)
    # behind the `?` help screen. BINDINGS is derived from HOTKEYS, so every entry is a Binding.
    shown = {b.key for b in Dashboard.BINDINGS if b.show}
    hidden = {b.key for b in Dashboard.BINDINGS if not b.show}
    assert shown == {"t", "n", "x", "/", "d", "question_mark", "q"}
    assert hidden == {"r", "R", "p", "g", "a", "s", "u", "y", "Y", "escape"}


def test_bindings_and_help_derive_from_the_single_hotkey_table() -> None:
    # The DRY invariant: the footer bindings and the help screen are *both* derived from HOTKEYS,
    # so the keymap can't drift between them. Every binding traces back to a HOTKEYS entry, and
    # every entry's action resolves to an action_* method on the dashboard (or Textual's built-in
    # quit) — so a stale action name can't slip in.
    assert [b.key for b in Dashboard.BINDINGS] == [h.key for h in dashboard.HOTKEYS]
    shown = {h.key for h in dashboard.HOTKEYS if h.show}
    assert {b.key for b in Dashboard.BINDINGS if b.show} == shown
    for hotkey in dashboard.HOTKEYS:
        assert hotkey.action == "quit" or hasattr(Dashboard, f"action_{hotkey.action}")


async def test_pressing_question_mark_opens_the_help_screen() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.HelpScreen)


async def test_help_screen_lists_every_hotkey() -> None:
    # The help screen is the authoritative keymap: every entry in HOTKEYS (key + description)
    # must render, so a future binding change can't quietly drop a key from the listing.
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        text = str(app.screen.query_one("#help-keys", Static).render())
        for hotkey in dashboard.HOTKEYS:
            assert hotkey.description in text
            assert (hotkey.display or hotkey.key) in text
        # the non-essential keys (hidden from the footer) are reachable here
        assert {"r", "R", "p", "g", "a", "s", "u"} <= {h.key for h in dashboard.HOTKEYS}


async def test_help_screen_closes_on_escape() -> None:
    app = Dashboard(_FakeClient([_TASK]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, dashboard.HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # dismissed — back to the task view
        assert app.is_running


# -- governor grouping (_group_by_governor / _slug_cell prefix) ----------------------


def test_group_by_governor_ungoverned_tasks_unchanged() -> None:
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None}
    active, terminal = _group_by_governor([t1, t2])
    assert [(t["id"], p) for t, p in active] == [("t1", ""), ("t2", "")]
    assert terminal == []


def test_group_by_governor_governed_task_appears_after_governor() -> None:
    # Both active; governed is the only (last) child → gets "└─ " connector.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    active, terminal = _group_by_governor([governor, governed])
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_governed_before_governor_in_sort_still_groups() -> None:
    # Governed slug sorts before the governor, but it must still appear AFTER the governor.
    governor = {**_TASK, "id": "gov", "slug": "zoo", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "alpha", "governor_task_id": "gov"}
    sorted_tasks = sorted([governor, governed], key=_sort_key)
    assert sorted_tasks[0]["id"] == "wrk"  # governed sorts first alphabetically
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_governor_not_in_list_behaves_as_root() -> None:
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "missing-id"}
    active, terminal = _group_by_governor([governed])
    assert [(t["id"], p) for t, p in active] == [("wrk", "")]
    assert terminal == []


def test_group_by_governor_terminal_governed_follows_active_governor() -> None:
    # A governed task in COMPLETE state is pulled into the active section when its governor
    # is still active, so it nests under the governor above the divider.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None, "state": "WORKING"}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov", "state": "COMPLETE"}
    sorted_tasks = sorted([governor, governed], key=_sort_key)
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("wrk", "└─ ")]
    assert terminal == []


def test_group_by_governor_all_terminal_no_governor_stays_terminal() -> None:
    # Two unrelated terminal tasks: no governor chain → both stay in the terminal section.
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None, "state": "COMPLETE"}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None, "state": "DROPPED"}
    active, terminal = _group_by_governor([t1, t2])
    assert active == []
    assert [(t["id"], p) for t, p in terminal] == [("t1", ""), ("t2", "")]


def test_group_by_governor_multiple_governed_tasks_in_sort_order() -> None:
    governor = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    w1 = {**_TASK, "id": "w1", "slug": "alpha", "governor_task_id": "gov"}
    w2 = {**_TASK, "id": "w2", "slug": "bravo", "governor_task_id": "gov"}
    sorted_tasks = sorted([governor, w1, w2], key=_sort_key)
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [("gov", ""), ("w1", "├─ "), ("w2", "└─ ")]
    assert terminal == []


def test_group_by_governor_tree_connectors_nested() -> None:
    # Governor → child-1 (non-last) → grandchild; Governor → child-2 (last).
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    c1 = {**_TASK, "id": "c1", "slug": "child-1", "governor_task_id": "gov"}
    gc = {**_TASK, "id": "gc", "slug": "grand", "governor_task_id": "c1"}
    c2 = {**_TASK, "id": "c2", "slug": "child-2", "governor_task_id": "gov"}
    sorted_tasks = sorted([gov, c1, gc, c2], key=_sort_key)
    active, terminal = _group_by_governor(sorted_tasks)
    assert [(t["id"], p) for t, p in active] == [
        ("gov", ""),
        ("c1", "├─ "),
        ("gc", "│  └─ "),
        ("c2", "└─ "),
    ]
    assert terminal == []


def test_slug_cell_prefix_tree_connectors() -> None:
    task = {**_TASK, "slug": "worker", "memo": None}
    assert _slug_cell(task).plain == "worker"             # no prefix (root)
    assert _slug_cell(task, "├─ ").plain == "├─ worker"   # non-last child
    assert _slug_cell(task, "└─ ").plain == "└─ worker"   # last child
    assert _slug_cell(task, "│  └─ ").plain == "│  └─ worker"  # nested


def test_slug_cell_prefix_with_memo() -> None:
    task = {"slug": "worker", "memo": "fix it"}
    assert _slug_cell(task, "├─ ").plain == "├─ worker[fix it]"


async def test_governed_task_appears_under_governor_in_dashboard() -> None:
    # Governor and governed both active; governed follows governor with a tree connector.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        order = [str(k.value) for k in table.rows]
        assert order == ["gov", "wrk"]
        gov_row = table.get_row("gov")
        wrk_row = table.get_row("wrk")
        assert gov_row[4].plain == "orchestrator"        # slug column (index 4) — no prefix
        assert wrk_row[4].plain == "└─ worker"           # last (only) child gets └─


async def test_separator_after_group_not_mid_group() -> None:
    # Regression: a terminal governed task whose governor is still active must stay in the
    # active section, so the separator appears *after* the whole group — not between the
    # governor and its child.
    governor = {
        **_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None,
        "state": "WORKING",
    }
    governed = {
        **_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov",
        "state": "COMPLETE", "turn": "agent",
    }
    other_done = {
        **_TASK, "id": "done", "slug": "other", "governor_task_id": None,
        "state": "COMPLETE", "turn": "agent",
    }
    tasks = sorted([governor, governed, other_done], key=_sort_key)
    app = Dashboard(_FakeClient(tasks))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        keys = [str(k.value) for k in app.query_one("#tasks", DataTable).rows]
        # Governor and its terminal child both appear above the divider; unrelated terminal
        # task appears below it.
        assert keys.index("gov") < keys.index(_SEPARATOR_KEY)
        assert keys.index("wrk") < keys.index(_SEPARATOR_KEY)
        assert keys.index("done") > keys.index(_SEPARATOR_KEY)


# -- ensemble collapse (_group_section collapsed / Dashboard Enter) ---------------


def test_ensemble_row_replaces_children_when_collapsed() -> None:
    # A collapsed governor's single child becomes one ensemble placeholder row.
    governor = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    worker = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    result = _group_section([governor, worker], collapsed={"gov"})
    assert len(result) == 2
    gov_row, ens_row = result
    assert gov_row[0]["id"] == "gov"
    assert ens_row[0].get("_ensemble") is True
    assert ens_row[0]["_governor_id"] == "gov"
    assert ens_row[0]["_count"] == 1
    assert ens_row[1] == "└─ "  # sole child → last-child connector


def test_ensemble_row_multiple_children_collapsed() -> None:
    # All direct children collapse to one ensemble row; count reflects original child count.
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    w1 = {**_TASK, "id": "w1", "slug": "alpha", "governor_task_id": "gov"}
    w2 = {**_TASK, "id": "w2", "slug": "bravo", "governor_task_id": "gov"}
    w3 = {**_TASK, "id": "w3", "slug": "charlie", "governor_task_id": "gov"}
    result = _group_section([gov, w1, w2, w3], collapsed={"gov"})
    assert len(result) == 2  # governor + one ensemble row (not three child rows)
    ens = result[1][0]
    assert ens["_ensemble"] is True
    assert ens["_count"] == 3


def test_ensemble_not_emitted_when_no_children() -> None:
    # Collapsed flag on a non-governor (no children) is silently ignored — no ensemble row.
    solo = {**_TASK, "id": "solo", "slug": "lone", "governor_task_id": None}
    result = _group_section([solo], collapsed={"solo"})
    assert len(result) == 1
    assert not result[0][0].get("_ensemble")


def test_ensemble_connector_inherits_parent_continuation() -> None:
    # When a nested governor (itself a child) is collapsed, its ensemble row picks up the
    # parent's continuation bars in the prefix.
    root = {**_TASK, "id": "root", "slug": "root", "governor_task_id": None}
    mid = {**_TASK, "id": "mid", "slug": "mid", "governor_task_id": "root"}
    leaf = {**_TASK, "id": "leaf", "slug": "leaf", "governor_task_id": "mid"}
    # Collapse only the middle level.
    result = _group_section([root, mid, leaf], collapsed={"mid"})
    rows = [(r["id"] if not r.get("_ensemble") else "__ensemble__", p) for r, p in result]
    assert rows == [
        ("root", ""),
        ("mid", "└─ "),   # mid is last (and only) child of root → └─
        ("__ensemble__", "   └─ "),  # ensemble is child of mid; mid is last child → "   " continuation
    ]


def test_ensemble_unexpanded_shows_real_rows() -> None:
    # With no collapsed governors, the output is identical to the non-collapsed path.
    gov = {**_TASK, "id": "gov", "slug": "orch", "governor_task_id": None}
    wrk = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    expanded = _group_section([gov, wrk], collapsed=set())
    assert [(r["id"], p) for r, p in expanded] == [("gov", ""), ("wrk", "└─ ")]


def test_matches_always_passes_ensemble_rows() -> None:
    ensemble: dict[str, Any] = {"_ensemble": True, "_governor_id": "gov", "_count": 2}
    assert _matches(ensemble, "some-query-that-wont-match")
    assert _matches(ensemble, "")


async def test_enter_on_governor_collapses_to_ensemble_row() -> None:
    # Pressing Enter on a governing task replaces its children with an ensemble row.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        # Initial state: both rows present.
        assert "gov" in [str(k.value) for k in table.rows]
        assert "wrk" in [str(k.value) for k in table.rows]
        # Move to the governor row and press Enter.
        table.move_cursor(row=table.get_row_index("gov"))
        await pilot.press("enter")
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        # Governor is still there; worker is gone; ensemble sentinel is present.
        assert "gov" in keys
        assert "wrk" not in keys
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in keys
        # The ensemble row's slug cell reads "..." (dim, checked by plain text).
        ens_row = table.get_row(f"{_ENSEMBLE_KEY_PREFIX}gov")
        assert ens_row[4].plain == "└─ ..."


async def test_enter_again_on_governor_expands_ensemble() -> None:
    # Pressing Enter twice returns to the fully-expanded tree.
    governor = {**_TASK, "id": "gov", "slug": "orchestrator", "governor_task_id": None}
    governed = {**_TASK, "id": "wrk", "slug": "worker", "governor_task_id": "gov"}
    app = Dashboard(_FakeClient([governor, governed]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        table.move_cursor(row=table.get_row_index("gov"))
        # First Enter → collapse.
        await pilot.press("enter")
        await pilot.pause()
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" in [str(k.value) for k in table.rows]
        # Second Enter → expand.
        await pilot.press("enter")
        await pilot.pause()
        keys = [str(k.value) for k in table.rows]
        assert "wrk" in keys
        assert f"{_ENSEMBLE_KEY_PREFIX}gov" not in keys


async def test_enter_on_non_governor_does_nothing() -> None:
    # Pressing Enter on a task with no governed children leaves the table unchanged.
    t1 = {**_TASK, "id": "t1", "slug": "alpha", "governor_task_id": None}
    t2 = {**_TASK, "id": "t2", "slug": "bravo", "governor_task_id": None}
    app = Dashboard(_FakeClient([t1, t2]))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", DataTable)
        before = [str(k.value) for k in table.rows]
        table.move_cursor(row=table.get_row_index("t1"))
        await pilot.press("enter")
        await pilot.pause()
        after = [str(k.value) for k in table.rows]
        assert before == after
