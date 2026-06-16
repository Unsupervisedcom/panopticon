"""The Textual dashboard (ADR 0002 presentation adapter): the operator's view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right. Keys: `r`
refreshes from the task service over REST, `t` hands off to the task's container tmux, `n`
creates a task (pick repo → workflow), and `x` **drops** it. Drop is the only state transition
the dashboard drives: every other transition starts a new agentic turn, so it's triggered by an
in-container agent skill (advance/iterate over REST/MCP), not the operator (ADR 0004).

The dashboard does not attach to tmux itself: on `t` it calls ``on_switch`` (the terminal
supervisor, ADR 0009 §6, records the chosen session and detaches this client) and **keeps
running**, so when the supervisor re-attaches after the operator detaches the task, it is the
same live dashboard — cursor and all. Network calls are synchronous (small, local); moving them
to Textual workers is a refinement (docs/BACKLOG.md).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static

from panopticon.client import JsonObj, TaskServiceClient


def _short(task_id: str) -> str:
    return task_id[:8]


def render_detail(task: JsonObj) -> str:
    """The right-pane text for one task: identity, state/turn, and history."""
    turn = f"{task['turn']}{' (blocked)' if task.get('blocked') else ''}"
    lines = [
        f"[b]{task.get('slug') or task['id']}[/b]",
        f"state: {task['state']}    turn: {turn}    workflow: {task['workflow']}",
        "",
        "history:",
    ]
    for entry in task["history"]:
        line = f"  {entry['from_state'] or '∅'} → {entry['to_state']}"
        if entry.get("trigger"):
            line += f" ({entry['trigger']})"
        responsibilities = entry.get("responsibilities") or []
        if responsibilities:
            line += "  " + ", ".join(f"{r['key']}={r['status']}" for r in responsibilities)
        lines.append(line)
    return "\n".join(lines)


class ChoiceScreen(ModalScreen[str | None]):
    """A modal list picker: select an option (Enter) or cancel (Escape); dismisses the choice."""

    CSS = """
    ChoiceScreen { align: center middle; }
    #choice-box { width: 48; height: auto; max-height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            yield Label(self._title)
            yield OptionList(*self._options)

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)


class Dashboard(App[None]):
    """The task view. On `t` it calls ``on_switch`` with the task's session and stays running;
    the supervisor handles the attach/detach (ADR 0009)."""

    CSS = "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; }"
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("n", "new_task", "New task"),
        ("x", "drop", "Drop"),
        ("t", "attach", "Attach tmux"),
        ("q", "quit", "Quit"),
    ]
    TITLE = "panopticon"

    def __init__(
        self, client: TaskServiceClient, *, on_switch: Callable[[str], None] | None = None
    ) -> None:
        super().__init__()
        self._client = client
        self._on_switch = on_switch  # supervisor hook: record the pick + detach (None standalone)
        self._tasks: dict[str, JsonObj] = {}
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="tasks")
            yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "slug", "state", "turn")
        self.action_refresh()

    def action_refresh(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.clear()
        self._tasks = {t["id"]: t for t in self._client.list_tasks()}
        for task in self._tasks.values():
            turn = f"{task['turn']} ⚠" if task.get("blocked") else task["turn"]
            table.add_row(
                _short(task["id"]), task["slug"] or "-", task["state"], turn,
                key=task["id"],
            )
        self._update_detail(next(iter(self._tasks), None))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._update_detail(str(key) if key is not None else None)

    def _update_detail(self, task_id: str | None) -> None:
        self._current = task_id
        task = self._tasks.get(task_id) if task_id else None
        self.query_one("#detail", Static).update(render_detail(task) if task else "no tasks")

    def action_new_task(self) -> None:
        """`n`: create a task — pick a repo, then a workflow, then POST it and refresh."""
        repos = [str(r["id"]) for r in self._client.list_repos()]
        workflows = self._client.list_workflows()
        if not repos or not workflows:
            self.notify("Need at least one repo and workflow to create a task.", severity="warning")
            return

        def pick_workflow(repo: str | None) -> None:
            if repo is None:
                return

            def create(workflow: str | None) -> None:
                if workflow is None:
                    return
                self._client.create_task(repo, workflow)
                self.action_refresh()

            self.push_screen(ChoiceScreen("workflow", workflows), create)

        self.push_screen(ChoiceScreen("repo", repos), pick_workflow)

    def action_drop(self) -> None:
        """`x`: abandon the highlighted task. Drop is the **only** transition the dashboard
        drives — every other transition starts a new agentic turn, so it's triggered by an
        in-container agent skill, not the operator (ADR 0004)."""
        task_id = self._current
        if task_id is None:
            return
        try:
            self._client.apply_operation(task_id, "drop")
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", str(exc))
            self.notify(f"Can't drop: {detail}", severity="error")
            return
        self.action_refresh()

    def action_attach(self) -> None:
        """`t`: hand off to the highlighted task's container tmux session, if it's running.

        Calls ``on_switch`` (the supervisor records the session and detaches this client, then
        attaches the task) and **keeps running**, so returning lands on this same live dashboard
        (ADR 0009). Switching is always detach→attach, never `switch-client`. Standalone (no
        supervisor) there is nothing to attach to."""
        if self._current is None:
            return
        if self._on_switch is None:
            self.notify("Attach is available when run via `panopticon console`.", severity="warning")
            return
        registrations = self._client.list_registrations(self._current)
        if not registrations:
            self.notify("No running container for this task.", severity="warning")
            return
        self._on_switch(registrations[0]["container_id"])  # session == container id (runner names it)


def run(client: TaskServiceClient, *, on_switch: Callable[[str], None] | None = None) -> None:
    """Run the dashboard. ``on_switch`` is the supervisor's `t` hook (ADR 0009); ``None`` standalone."""
    Dashboard(client, on_switch=on_switch).run()
