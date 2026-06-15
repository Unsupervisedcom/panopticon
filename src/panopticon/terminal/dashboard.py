"""The Textual dashboard (ADR 0002 presentation adapter): the operator's view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right. Keys: `r`
refreshes from the task service over REST, `t` attaches to the task's container tmux, `n`
creates a task (pick repo → workflow), `a` advances it (pick a legal next state). The pickers
are modal choice lists; the legal next states come from the service so the operator can't pick
an illegal one. Network calls are synchronous (small, local); moving them to Textual workers is
a refinement (docs/BACKLOG.md).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static

from panopticon.sessionservice.local_runner import TMUX_SOCKET
from panopticon.terminal.attach import attach_command
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
    CSS = "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; }"
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("n", "new_task", "New task"),
        ("a", "advance", "Advance"),
        ("t", "attach", "Attach tmux"),
        ("q", "quit", "Quit"),
    ]
    TITLE = "panopticon"

    def __init__(self, client: DashboardClient, *, attach: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._client = client
        self._tasks: dict[str, JsonObj] = {}
        self._current: str | None = None
        self._attacher = attach or self._attach_session  # injectable for tests

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

    def action_advance(self) -> None:
        """`a`: move the highlighted task to one of its legal next states (chosen from a picker)."""
        task_id = self._current
        if task_id is None:
            return
        states = self._client.list_transitions(task_id)
        if not states:
            self.notify("No transitions available from this state.", severity="warning")
            return

        def apply(state: str | None) -> None:
            if state is None:
                return
            self._client.request_transition(task_id, state)
            self.action_refresh()

        self.push_screen(ChoiceScreen("advance to", states), apply)

    def action_attach(self) -> None:
        """`t`: switch into the highlighted task's container tmux session, if it's running."""
        if self._current is None:
            return
        registrations = self._client.list_registrations(self._current)
        if not registrations:
            self.notify("No running container for this task.", severity="warning")
            return
        self._attacher(registrations[0]["container_id"])  # session == container id (runner names it so)

    def _attach_session(self, session: str) -> None:
        inside_tmux = bool(os.environ.get("TMUX"))
        command = attach_command(session, socket=TMUX_SOCKET, inside_tmux=inside_tmux)
        if inside_tmux:
            subprocess.run(command, check=False)  # switch this client; no terminal handover
        else:
            with self.suspend():  # hand the terminal to tmux, resume on detach
                subprocess.run(command, check=False)


def run(client: DashboardClient) -> None:
    """Launch the interactive dashboard."""
    Dashboard(client).run()
