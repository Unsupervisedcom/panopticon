"""The Textual dashboard (ADR 0002 presentation adapter): the operator's view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right. It
auto-refreshes from the task service every ``REFRESH_INTERVAL`` seconds (preserving the
highlighted row across the rebuild); `r` forces a refresh now. Keys: `r`
refreshes from the task service over REST, `t` hands off to the task's container tmux, `n`
creates a task (pick repo → workflow → describe the work), `x` **drops** it, `R` **respawns** a down task (releases
its claim so the host runner re-spawns it), `p` opens the task's `url` in the browser
(cloude-cade's `p` "open PR"), and `g` opens the **repo config screen** (list / create / edit repos). Drop is the only state *transition* the dashboard
drives: every other transition starts a new agentic turn, so it's triggered by an in-container
agent skill (advance/iterate over REST/MCP), not the operator (ADR 0004).

The `run` column shows each task's container status: `live` (an active registration), `down`
(was up, container gone — respawn with `R`), `starting` (claimed, no registration yet — its
container is still coming up), `–` (unclaimed/not spawned yet), or `respawning` (just released
by `R`, awaiting the runner's re-claim). Liveness is the registration, independent of provisioning.

The dashboard does not attach to tmux itself: on `t` it calls ``on_switch`` (the terminal
supervisor, ADR 0009 §6, records the chosen session and detaches this client) and **keeps
running**, so when the supervisor re-attaches after the operator detaches the task, it is the
same live dashboard — cursor and all. Network calls are synchronous (small, local); moving them
to Textual workers is a refinement (docs/BACKLOG.md).
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import Any

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, Static

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.state import TERMINAL_LABELS


def _sort_key(task: JsonObj) -> tuple[bool, str, str]:
    """Order rows by state, sinking terminal states (COMPLETE/DROPPED) to the bottom.

    Active work sorts to the top (alphabetically by state); finished tasks settle below it.
    Ties break on slug (then id) for a stable, readable order.
    """
    state = task["state"]
    return (state in TERMINAL_LABELS, state, task["slug"] or task["id"])


def _short(task_id: str) -> str:
    return task_id[:8]


# Turn-column colors, matching cloude-cade's dashboard ball tags: agent=green,
# user=yellow, blocked=red. Blocked takes precedence (cloude-cade draws it as its own
# red tag); here it keeps the turn value but appends ⚠ and colors the whole cell red.
def _turn_cell(task: JsonObj) -> Text:
    if task.get("blocked"):
        return Text(f"{task['turn']} ⚠", style="red")
    color = "green" if task["turn"] == "agent" else "yellow"
    return Text(task["turn"], style=color)


def render_detail(task: JsonObj) -> str:
    """The right-pane text for one task: identity, state/turn, and history."""
    turn = f"{task['turn']}{' (blocked)' if task.get('blocked') else ''}"
    claim = f"    claimed: {task['claimed_by']}" if task.get("claimed_by") else ""
    lines = [
        f"[b]{task.get('slug') or task['id']}[/b]",
        f"state: {task['state']}    turn: {turn}    workflow: {task['workflow']}{claim}",
    ]
    if task.get("description"):
        lines += ["", task["description"]]
    if task.get("url"):
        lines += ["", f"url: {task['url']}"]
    lines += ["", "history:"]
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


class InputScreen(ModalScreen[str | None]):
    """A modal free-text prompt: submit the text (Enter) or cancel (Escape).

    Dismisses the entered string (empty string if blank) on submit, or ``None`` on cancel — so
    a caller can tell "left it empty" apart from "backed out"."""

    CSS = """
    InputScreen { align: center middle; }
    #input-box { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Label(self._title)
            yield Input()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RepoFormScreen(ModalScreen["dict[str, str] | None"]):
    """A modal form for a repo's core fields. Submits a ``{field: value}`` dict on save (Enter
    or Ctrl+S), or ``None`` on cancel (Escape).

    Create mode (no ``repo``): every field is an editable :class:`Input`, including ``id``.
    Edit mode: ``id`` is shown read-only (the primary key can't change) and the rest are
    pre-populated. Only the **core** fields are here; ``image_layer``/``capabilities`` aren't
    edited in the TUI, and a PATCH update leaves them untouched."""

    CSS = """
    RepoFormScreen { align: center middle; }
    #repo-form { width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #repo-form Input { margin-bottom: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "submit", "Save")]

    FIELDS = ("name", "git_url", "default_base", "env_file", "creds_volume")

    def __init__(self, title: str, repo: JsonObj | None = None) -> None:
        super().__init__()
        self._title = title
        self._repo = repo or {}
        self._editing = repo is not None

    def compose(self) -> ComposeResult:
        with Vertical(id="repo-form"):
            yield Label(self._title)
            if self._editing:
                yield Label(f"id: {self._repo['id']}")
            else:
                yield Input(placeholder="id", id="field-id")
            for name in self.FIELDS:
                yield Input(value=str(self._repo.get(name) or ""), placeholder=name, id=f"field-{name}")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        values: dict[str, str] = {}
        if not self._editing:
            values["id"] = self.query_one("#field-id", Input).value.strip()
        for name in self.FIELDS:
            values[name] = self.query_one(f"#field-{name}", Input).value.strip()
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReposScreen(ModalScreen[None]):
    """Repo management: list repos and create (`n`) / edit (`e`) them; Escape returns to the
    task view. Mutations go through the task service over REST, then the table refreshes."""

    CSS = """
    ReposScreen { align: center middle; }
    #repos-box { width: 90%; height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    """
    BINDINGS = [
        ("n", "new_repo", "New repo"),
        ("e", "edit_repo", "Edit repo"),
        ("escape", "close", "Close"),
    ]

    def __init__(self, client: TaskServiceClient) -> None:
        super().__init__()
        self._client = client
        self._repos: dict[str, JsonObj] = {}
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="repos-box"):
            yield Label("repos — n: new   e: edit   esc: close")
            yield DataTable(id="repos")

    def on_mount(self) -> None:
        table = self.query_one("#repos", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "name", "git_url", "default_base")
        table.focus()
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#repos", DataTable)
        table.clear()
        self._repos = {str(r["id"]): r for r in self._client.list_repos()}
        for repo in self._repos.values():
            table.add_row(
                repo["id"], repo["name"], repo["git_url"], repo["default_base"], key=str(repo["id"])
            )
        self._current = self._current if self._current in self._repos else next(iter(self._repos), None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._current = str(key) if key is not None else None

    def action_close(self) -> None:
        self.dismiss(None)

    def action_new_repo(self) -> None:
        def create(values: dict[str, str] | None) -> None:
            if values is None:  # backed out
                return
            if not (values["id"] and values["name"] and values["git_url"]):
                self.notify("id, name and git_url are required.", severity="warning")
                return
            try:
                self._client.create_repo(
                    values["id"], values["name"], values["git_url"], values["default_base"] or "main",
                    env_file=values["env_file"] or None, creds_volume=values["creds_volume"] or None,
                )
            except httpx.HTTPStatusError as exc:
                self.notify(f"Can't create: {_detail(exc)}", severity="error")
                return
            self._refresh()

        self.app.push_screen(RepoFormScreen("new repo"), create)

    def action_edit_repo(self) -> None:
        if self._current is None:
            return
        repo_id = self._current

        def save(values: dict[str, str] | None) -> None:
            if values is None:
                return
            try:  # PATCH: only the core fields move; image_layer/capabilities are left intact.
                self._client.update_repo(
                    repo_id, name=values["name"], git_url=values["git_url"],
                    default_base=values["default_base"] or "main",
                    env_file=values["env_file"] or None, creds_volume=values["creds_volume"] or None,
                )
            except httpx.HTTPStatusError as exc:
                self.notify(f"Can't update: {_detail(exc)}", severity="error")
                return
            self._refresh()

        self.app.push_screen(RepoFormScreen(f"edit {repo_id}", repo=self._repos[repo_id]), save)


def _detail(exc: httpx.HTTPStatusError) -> str:
    """The task service's error detail for a failed request (falls back to the bare error)."""
    try:
        return str(exc.response.json().get("detail", str(exc)))
    except ValueError:
        return str(exc)


class Dashboard(App[None]):
    """The task view. On `t` it calls ``on_switch`` with the task's session (and `s` calls
    ``on_service`` for the task-service session) and stays running; the supervisor handles the
    attach/detach (ADR 0009)."""

    CSS = "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; }"
    REFRESH_INTERVAL = 2.0  # seconds between automatic refreshes (0/None disables the timer)
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("n", "new_task", "New task"),
        ("x", "drop", "Drop"),
        ("R", "respawn", "Respawn"),
        ("t", "attach", "Attach tmux"),
        ("p", "open_url", "Open URL"),
        ("g", "repos", "Repos"),
        ("s", "service", "Service"),
        ("q", "quit", "Quit"),
    ]
    TITLE = "panopticon"

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        on_switch: Callable[[str], None] | None = None,
        on_service: Callable[[], bool] | None = None,
        refresh_interval: float | None = REFRESH_INTERVAL,
    ) -> None:
        super().__init__()
        self._client = client
        self._on_switch = on_switch  # supervisor hook: record the pick + detach (None standalone)
        self._on_service = on_service  # `s` hook: switch to the service session; True if one exists
        self._refresh_interval = refresh_interval  # auto-refresh cadence (0/None → manual only)
        self._tasks: dict[str, JsonObj] = {}
        self._current: str | None = None
        self._respawning: set[str] = set()  # tasks awaiting re-claim after `R` (shown "respawning")

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="tasks")
            yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "state", "turn", "run", "slug")
        self.action_refresh()
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self.action_refresh)

    def _run_status(self, task: JsonObj) -> str:
        """A task's container status: `live` (a registered container), `down` (was up, container
        gone — respawn with `R`), `starting` (claimed, container still coming up — no registration
        yet), `–` (unclaimed), or `respawning` (just released by `R`, awaiting the runner's re-claim —
        shown instead of the bare `–` so a respawn doesn't read as the task losing its runner).

        Liveness is the **registration**, not provisioning: a task can be live and working (e.g. an
        unprovisioned PLANNING task that hasn't set its slug yet) — so registration is checked first.
        """
        tid = task["id"]
        if not task.get("claimed_by"):
            return "respawning" if tid in self._respawning else "–"
        self._respawning.discard(tid)  # re-claimed → the normal down→live boot takes over
        if self._client.list_registrations(tid):
            return "live"  # a registered container — regardless of whether it's provisioned yet
        return "down" if task.get("provisioned") else "starting"

    def action_refresh(self) -> None:
        table = self.query_one("#tasks", DataTable)
        selected = self._current  # keep the operator's highlight across the rebuild (auto-refresh)
        table.clear()
        ordered = sorted(self._client.list_tasks(), key=_sort_key)  # state asc, terminal last
        self._tasks = {t["id"]: t for t in ordered}
        for task in ordered:
            table.add_row(
                _short(task["id"]), task["state"], _turn_cell(task), self._run_status(task),
                task["slug"] or "-",
                key=task["id"],
            )
        target = selected if selected in self._tasks else next(iter(self._tasks), None)
        if target is not None:
            table.move_cursor(row=table.get_row_index(target))
        self._update_detail(target)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._update_detail(str(key) if key is not None else None)

    def _update_detail(self, task_id: str | None) -> None:
        self._current = task_id
        task = self._tasks.get(task_id) if task_id else None
        self.query_one("#detail", Static).update(render_detail(task) if task else "no tasks")

    def action_new_task(self) -> None:
        """`n`: create a task — pick a repo, a workflow, describe the work, then POST it."""
        repos = [str(r["id"]) for r in self._client.list_repos()]
        workflows = self._client.list_workflows()
        if not repos or not workflows:
            self.notify("Need at least one repo and workflow to create a task.", severity="warning")
            return

        def pick_workflow(repo: str | None) -> None:
            if repo is None:
                return

            def describe(workflow: str | None) -> None:
                if workflow is None:
                    return

                def create(description: str | None) -> None:
                    if description is None:  # backed out of the prompt
                        return
                    self._client.create_task(repo, workflow, description.strip() or None)
                    self.action_refresh()

                self.push_screen(InputScreen("description"), create)

            self.push_screen(ChoiceScreen("workflow", workflows), describe)

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

    def action_respawn(self) -> None:
        """`R`: respawn a **down** task — release its claim so the host runner re-spawns it.

        Only for a task claimed by a runner with no live container; releasing a live task would
        double-spawn it, so that's refused. Unclaimed tasks have nothing to respawn."""
        task_id = self._current
        if task_id is None:
            return
        task = self._tasks.get(task_id)
        if not task or not task.get("claimed_by"):
            self.notify("Task isn't claimed by a runner — nothing to respawn.", severity="warning")
            return
        if self._client.list_registrations(task_id):
            self.notify("Container is live; drop it or let it finish.", severity="warning")
            return
        self._respawning.add(task_id)  # show "respawning" until the runner re-claims it
        self._client.release(task_id)  # back to unclaimed → the host runner re-claims + re-spawns
        self.notify("Released the claim; the runner will respawn it.")
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

    def action_open_url(self) -> None:
        """`p`: open the highlighted task's `url` in the browser (cloude-cade's `p` "open PR").

        Opens on the machine running the dashboard, like cloude-dash; a no-op with a notice when
        the task has no URL set."""
        if self._current is None:
            return
        task = self._tasks.get(self._current)
        url = task.get("url") if task else None
        if not url:
            self.notify("No URL set for this task.", severity="warning")
            return
        webbrowser.open(url)
        self.notify(f"opened {url}")

    def action_repos(self) -> None:
        """`g`: open the repo config screen — list repos and create/edit them (ADR 0002)."""
        self.push_screen(ReposScreen(self._client))

    def action_service(self) -> None:
        """`s`: switch to the task-service tmux session, when one is running (ADR 0009).

        The service is a sibling tmux session under `panopticon console`; ``on_service`` switches
        to it the same way `t` switches to a task (record + detach), returning whether a service
        session existed. Standalone (no supervisor) there is nothing to switch to."""
        if self._on_service is None:
            self.notify("Service shortcut is available when run via `panopticon console`.", severity="warning")
            return
        if not self._on_service():
            self.notify("No task-service session is running.", severity="warning")


def run(
    client: TaskServiceClient,
    *,
    on_switch: Callable[[str], None] | None = None,
    on_service: Callable[[], bool] | None = None,
) -> None:
    """Run the dashboard. ``on_switch``/``on_service`` are the supervisor's `t`/`s` hooks
    (ADR 0009); both ``None`` standalone."""
    Dashboard(client, on_switch=on_switch, on_service=on_service).run()
