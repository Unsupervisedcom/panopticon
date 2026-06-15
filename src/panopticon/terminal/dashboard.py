"""The Textual dashboard (ADR 0002 presentation adapter): a read-only view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right; `r` refreshes
from the task service over REST. Read-only for now — `t` (tmux attach) and input land in later
PRs of this slice. Network calls are synchronous (small, local); moving them to Textual
workers is a refinement (docs/BACKLOG.md).
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from panopticon.terminal.client import DashboardClient, JsonObj


def _short(task_id: str) -> str:
    return task_id[:8]


def render_detail(task: JsonObj) -> str:
    """The right-pane text for one task: identity, state/turn, and history."""
    lines = [
        f"[b]{task.get('slug') or task['id']}[/b]",
        f"state: {task['state']}    turn: {task['turn']}    workflow: {task['workflow']}",
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


class Dashboard(App[None]):
    CSS = "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; }"
    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]
    TITLE = "panopticon"

    def __init__(self, client: DashboardClient) -> None:
        super().__init__()
        self._client = client
        self._tasks: dict[str, JsonObj] = {}

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
            table.add_row(
                _short(task["id"]), task["slug"] or "-", task["state"], task["turn"],
                key=task["id"],
            )
        self._update_detail(next(iter(self._tasks), None))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._update_detail(str(key) if key is not None else None)

    def _update_detail(self, task_id: str | None) -> None:
        task = self._tasks.get(task_id) if task_id else None
        self.query_one("#detail", Static).update(render_detail(task) if task else "no tasks")


def run(client: DashboardClient) -> None:
    """Launch the interactive dashboard."""
    Dashboard(client).run()
