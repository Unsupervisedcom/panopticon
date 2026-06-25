"""The Textual dashboard (ADR 0002 presentation adapter): the operator's view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right. It refreshes
from the task service **on change** — a background worker long-polls the change feed
(``list_tasks_versioned``), so the table redraws within a round-trip of a state change and stays
still when nothing changes (no fixed-interval redraw); `r` forces a refresh now. The redraw
preserves the highlighted row across the rebuild. A dim divider row splits the active tasks from
the terminal (COMPLETE/DROPPED) ones that sink below them; the arrow keys jump over it (it's not a
selectable task).

The footer legend shows only the essential, most-used keys — `t` hands off to the task's
container tmux, `n` creates a task (pick repo → workflow → describe the work), `x` **drops** it,
`/` searches, `d` **toggles the detail pane** (hidden by default so the table gets the full
width, press to reveal it), `q` quits, and `?` opens the **help screen** (a modal listing every key). The
rest still work but are hidden from the legend (both the footer bindings and `HelpScreen` derive
from the single ``HOTKEYS`` keymap): `r` refreshes from the task service over REST, `R` **respawns**
a down task (releases its claim so the host runner re-spawns it), `p` opens the task's `url` in the
browser (cloude-cade's `p` "open PR"), `g` opens the **repo config screen** (list / create / edit
repos — and it **opens automatically on start when no repos are configured**, the first-run
nudge to add one), `s` switches to the task-service session, and `a` opens a modal listing the task's
artifacts — Enter opens the selected
one with the host's default handler (`xdg-open`/`open`) by fetching it over REST to a temp file, `e`
opens the on-disk file in place when the dashboard shares the artifact store, `y` **copies the
task's slug** and `Y` its **id** to the clipboard (OSC 52 + the host's `pbcopy`/`xclip`/`wl-copy`,
so it works on Linux and macOS). Drop is the only state
*transition* the dashboard drives: every other transition starts a new agentic turn, so it's
triggered by an in-container agent skill (`advance` over REST/MCP; going back to coding is a free
`set_state` move), not the operator (ADR 0004).

`/` enters **search-as-you-type** (cloude-cade's `/`): a query box reveals at the bottom and the
table filters live to tasks whose slug/state/workflow/memo contains the query
(case-insensitive substring). `Enter` **locks** the filter — the box hides and normal navigation
keys return while the filter stays applied; `Esc` **clears** it (from typing or locked). The
filter is applied in ``action_refresh``, so a change-feed refresh preserves it across rebuilds.

The `container` column shows each task's container status: `live` (an active registration), `down`
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

import functools
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.worker import get_current_worker

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.state import TERMINAL_LABELS
from panopticon.taskservice.artifacts_fs import DEFAULT_ARTIFACTS, FilesystemArtifactStore


def _sort_key(task: JsonObj) -> tuple[bool, bool, float, str]:
    """Order rows for the operator: live work first, then by whose turn it is, then recency.

    1. non-terminal before terminal — COMPLETE/DROPPED sink to the bottom;
    2. the user's turn before the agent's — tasks waiting on the operator surface first;
    3. most-recently-updated first — the latest history entry's timestamp.

    Recency is the latest history ``at``; turn flips / ``blocked`` / ``url`` changes don't append
    history, so only a state transition moves a task on this axis. Ties break on slug (then id)
    for a stable, readable order.
    """
    state = task["state"]
    last = task["history"][-1].get("at") if task["history"] else None
    recency = -datetime.fromisoformat(last).timestamp() if last else 0.0  # negate → newest first
    return (
        state in TERMINAL_LABELS,  # False (live) before True (terminal)
        task["turn"] != "user",  # False (user) before True (agent)
        recency,
        task["slug"] or task["id"],
    )


def _short_tokens(n: int | None) -> str:
    """A token count in short human form for the table: ``None``/0 (not yet reported) → ``-``,
    under 1000 shown as-is (``300``), otherwise scaled to ``K``/``M``/``B`` to one decimal
    (``1.2K``, ``1.1M``). Plain ``str`` — the output has no markup-special chars (unlike the
    slug cell), so Textual renders it verbatim."""
    if not n:
        return "-"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(n)


# A sentinel row key for the divider drawn between the active and terminal task groups (see
# action_refresh). It's not a real task id, so it's never in ``self._tasks`` — the highlight
# handler treats it as "no task selected" and the arrow keys jump over it.
_SEPARATOR_KEY = "__separator__"


def _separator_cells(columns: int) -> list[Text]:
    """A dim box-drawing rule, one cell per task-table column — the visual divider between the
    active tasks and the terminal (COMPLETE/DROPPED) ones that sink below them."""
    return [Text("─" * 8, style="dim") for _ in range(columns)]


def _slug_cell(task: JsonObj) -> Text:
    """The ``slug[memo]`` column: the slug followed by the task's memo in brackets.

    Bare slug when there's no memo; bare ``[memo]`` (no leading dash) when there's a
    memo but no slug; ``-`` only when neither is set.

    Returned as a Rich ``Text`` (like :func:`_turn_cell`), **not** a markup string: Textual renders
    bare ``str`` cells through console markup, which swallows the ``[…]`` — so a plain string would
    show just the bare slug (the bug that hid memos; the header had the same problem)."""
    slug = task.get("slug") or ""
    memo = task.get("memo")
    if memo:
        return Text(f"{slug}[{memo}]")
    return Text(slug or "-")


# Fields a search query matches against (cloude-cade filters on the task title; our nearest
# analogs are the task's identifying text). Joined and lowercased into one haystack per task.
_SEARCH_FIELDS = ("slug", "state", "workflow", "memo")


def _matches(task: JsonObj, query: str) -> bool:
    """Case-insensitive substring match of ``query`` against a task's identifying fields.

    An empty query matches everything (no filter). Mirrors cloude-cade's "title contains the
    query" — plain substring, no fuzzy ranking."""
    if not query:
        return True
    haystack = " ".join(str(task.get(field) or "") for field in _SEARCH_FIELDS).lower()
    return query.lower() in haystack


# Turn-column colors, matching cloude-cade's dashboard ball tags: agent=green,
# user=yellow, blocked=red. Blocked takes precedence (cloude-cade draws it as its own
# red tag); here it keeps the turn value but appends ⚠ and colors the whole cell red.
def _turn_cell(task: JsonObj) -> Text:
    if task.get("blocked"):
        return Text(f"{task['turn']} ⚠", style="red")
    color = "green" if task["turn"] == "agent" else "yellow"
    return Text(task["turn"], style=color)


# Container-status colors. The status is composed by the **task service** (folding the session
# service's reported spawn phase with registration presence + runner liveness) and the dashboard
# just displays it: green = live; yellow = a spawn in flight (queued → … → awaiting); cyan = the
# runner is self-healing an orphan (respawning a recovered task); red = needs attention
# (down/failed/disconnected); the em-dash (terminal task) is dimmed.
_STATUS_COLORS = {
    "live": "green",
    "queued": "yellow",
    "healing": "cyan",
    "claiming": "yellow",
    "preparing": "yellow",
    "building": "yellow",
    "starting": "yellow",
    "awaiting": "yellow",
    "down": "red",
    "failed": "red",
    "disconnected": "red",
}


def _status_cell(task: JsonObj) -> Text:
    """The container column: the task service's composed ``container_status``, color-coded."""
    status = task.get("container_status") or "–"
    return Text(status, style=_STATUS_COLORS.get(status, "dim"))


def render_detail(task: JsonObj) -> str:
    """The right-pane text for one task: identity, state/turn, and history."""
    turn = f"{task['turn']}{' (blocked)' if task.get('blocked') else ''}"
    claim = f"    claimed: {task['claimed_by']}" if task.get("claimed_by") else ""
    lines = [
        f"[b]{task.get('slug') or task['id']}[/b]",
        f"id: {task['id']}",
        f"state: {task['state']}    turn: {turn}    workflow: {task['workflow']}{claim}",
    ]
    status = task.get("container_status")
    if status:
        detail = task.get("lifecycle_detail")
        lines += ["", f"container: {status}" + (f" — {detail}" if detail else "")]
    if task.get("memo"):
        lines += ["", task["memo"]]
    if task.get("url"):
        lines += ["", f"url: {task['url']}"]
    if task.get("tokens_used") or task.get("token_estimate"):
        used = _short_tokens(task.get("tokens_used"))
        est = _short_tokens(task.get("token_estimate"))
        lines += ["", f"tokens: {used} used / {est} est"]
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


def _open_command() -> str:
    """The host's "open this file with its default handler" command: `open` on macOS,
    `xdg-open` elsewhere (Linux + other freedesktop desktops)."""
    return "open" if sys.platform == "darwin" else "xdg-open"


def _open_path(path: str) -> None:
    """Hand ``path`` to the host's default handler, non-blocking (don't freeze the TUI). Raises
    ``FileNotFoundError`` when the opener isn't installed (e.g. headless host, no ``xdg-open``);
    callers catch it and notify rather than letting it crash the TUI."""
    subprocess.Popen([_open_command(), path])


# Linux clipboard writers, in preference order: Wayland first, then the X11 tools. Each is the
# full argv that reads the text to copy from stdin. macOS uses `pbcopy` unconditionally (it's
# always present), so it isn't in this list — see `_clipboard_command`.
_LINUX_CLIPBOARD_COMMANDS: tuple[list[str], ...] = (
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


@functools.cache
def _clipboard_command() -> list[str] | None:
    """The host's "write stdin to the system clipboard" command, or ``None`` when no clipboard
    tool is installed. ``pbcopy`` on macOS (always present); on Linux/other the first available
    of ``wl-copy`` (Wayland), ``xclip``, then ``xsel`` — mirrors :func:`_open_command`'s
    per-platform choice, but the Linux tools aren't guaranteed installed, hence the ``which``.

    Cached: the installed tool can't change over a dashboard's lifetime, so the ``PATH`` probe
    runs once (call ``_clipboard_command.cache_clear()`` to re-probe — only tests need to)."""
    if sys.platform == "darwin":
        return ["pbcopy"]
    for command in _LINUX_CLIPBOARD_COMMANDS:
        if shutil.which(command[0]):
            return command
    return None


def _clipboard_copy(text: str) -> bool:
    """Best-effort write of ``text`` to the host's system clipboard via :func:`_clipboard_command`,
    feeding the text on stdin. Returns whether a clipboard tool actually ran (``False`` when none
    is installed or the command failed) — the caller pairs this with an OSC 52 emit, so a ``False``
    here just means that path was unavailable, not that the copy as a whole failed."""
    command = _clipboard_command()
    if command is None:
        return False
    try:
        subprocess.run(command, input=text.encode(), check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _open_via_rest(client: TaskServiceClient, task_id: str, name: str, tmpdir: str) -> None:
    """Fetch an artifact over REST and open it: write its bytes under ``tmpdir`` (keeping the
    artifact's basename so the extension drives the handler), then open that. Works even when the
    dashboard is remote from the artifact store. ``tmpdir`` is the app's single reused scratch dir
    (cleaned on exit), so opens don't leak a directory each."""
    content = client.get_artifact(task_id, name)
    path = Path(tmpdir) / task_id / Path(name).name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _open_path(str(path))


_ResultT = TypeVar("_ResultT")


class _OptionListModal(ModalScreen[_ResultT | None]):
    """Shared skeleton for the modal list-pickers: a titled ``OptionList`` in a bordered box,
    Escape to cancel. Subclasses fix the result type, the box id/CSS, how a selection dismisses,
    and any extra widgets (e.g. a hint line)."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    BOX_ID = "list-box"

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id=self.BOX_ID):
            yield Label(self._title)
            yield OptionList(*self._options)
            yield from self._extra_widgets()

    def _extra_widgets(self) -> Iterable[Widget]:
        return ()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(_OptionListModal[str]):
    """A modal list picker: select an option (Enter) or cancel (Escape); dismisses the choice."""

    CSS = """
    ChoiceScreen { align: center middle; }
    #choice-box { width: 48; height: auto; max-height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    """
    BOX_ID = "choice-box"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))


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


def _repo_name_from_git_url(url: str) -> str:
    """The repository name from a git URL, for auto-filling the repo form.

    Handles HTTPS (``https://host/owner/repo.git``) and scp-style SSH
    (``git@host:owner/repo.git``): the last ``/``- or ``:``-delimited segment with any
    ``.git`` suffix and trailing slash stripped. Returns ``""`` when there's nothing
    parseable (empty, or a bare token with no path), so callers can no-op."""
    url = url.strip().rstrip("/")
    if not url or ("/" not in url and ":" not in url):
        return ""
    tail = re.split(r"[/:]", url)[-1]
    return tail[:-len(".git")] if tail.endswith(".git") else tail


class SpaceCheckbox(Checkbox, inherit_bindings=False):
    """A :class:`Checkbox` that toggles on **Space only** (Textual's default binds ``enter,space``).
    Dropping Enter lets the key bubble up to the screen, so Enter saves the form even while the
    checkbox holds focus — the form's Space-toggles / Enter-saves contract. ``inherit_bindings=False``
    keeps the base ``enter,space`` toggle binding from being merged back in; ``ToggleButton`` is
    its only source, so re-declaring ``space`` is the whole keymap."""

    BINDINGS = [Binding("space", "toggle_button", "Toggle", show=False)]


class RepoFormScreen(ModalScreen["dict[str, Any] | None"]):
    """A modal form for a repo's fields. Submits a ``{field: value}`` dict on save (Enter
    or Ctrl+S), or ``None`` on cancel (Escape). The text fields are strings; the privileged
    toggle is the bool ``docker_in_docker``.

    **Space toggles the checkbox; Enter saves the form** — from any field, including while the
    privileged-docker checkbox holds focus (it's a :class:`SpaceCheckbox`, so Enter bubbles up
    to the screen's submit binding rather than toggling). A footer hint spells this out.

    The **git URL leads** the form. In **create mode** the still-blank ``id``, ``name`` and
    ``creds_volume`` (a ``<repo>-creds`` convention) auto-fill from it when the URL field loses
    focus and again at submit — never clobbering a value the user already typed — and
    ``default_base`` defaults to ``main``. Edit mode applies neither: a repo's existing values
    are left exactly as they are.

    Create mode (no ``repo``): every field is an editable :class:`Input`, including ``id``.
    Edit mode: ``id`` is shown read-only (the primary key can't change) and the rest are
    pre-populated. The **privileged docker** checkbox maps to the repo's
    ``capabilities["docker_in_docker"]`` (runs the task container ``--privileged``); ``image_layer_file``
    and any other capability keys aren't edited in the TUI and a PATCH update leaves them untouched."""

    CSS = """
    RepoFormScreen { align: center middle; }
    #repo-form { width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #repo-form Input { margin-bottom: 1; }
    #repo-form Checkbox { margin-bottom: 1; }
    """
    # Enter saves from any field. Text Inputs consume Enter via their own submit binding (posting
    # Input.Submitted → on_input_submitted), so this screen binding only fires for fields that
    # don't — the SpaceCheckbox and the read-only id Label — and never double-saves.
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "submit", "Save"),
        ("ctrl+s", "submit", "Save"),
    ]

    # git_url leads (the auto-fill source); the rest follow. ``id`` is rendered between git_url
    # and these, separately, since it's editable only in create mode.
    FIELDS = ("git_url", "name", "default_base", "creds_volume", "env_file")
    # Fields auto-derived from git_url → how to derive each (create mode only; see
    # _autofill_from_git_url). id and name are the bare repo name; creds_volume a convention.
    _DERIVED: dict[str, Callable[[str], str]] = {
        "id": lambda repo: repo,
        "name": lambda repo: repo,
        "creds_volume": lambda repo: f"{repo}-creds",
    }

    def __init__(self, title: str, repo: JsonObj | None = None) -> None:
        super().__init__()
        self._title = title
        self._repo = repo or {}
        self._editing = repo is not None

    def _initial(self, name: str) -> str:
        """A field's pre-populated value: the repo's stored value, else (create mode only)
        ``main`` for ``default_base``, else blank."""
        stored = self._repo.get(name)
        if stored:
            return str(stored)
        return "main" if name == "default_base" and not self._editing else ""

    def compose(self) -> ComposeResult:
        with Vertical(id="repo-form"):
            yield Label(self._title)
            yield Input(value=self._initial("git_url"), placeholder="git_url", id="field-git_url")
            if self._editing:
                yield Label(f"id: {self._repo['id']}")
            else:
                yield Input(placeholder="id", id="field-id")
            for name in self.FIELDS[1:]:  # git_url already rendered above
                yield Input(value=self._initial(name), placeholder=name, id=f"field-{name}")
            yield SpaceCheckbox(
                "privileged docker (docker-in-docker)",
                value=bool(self._repo.get("capabilities", {}).get("docker_in_docker")),
                id="field-docker_in_docker",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _autofill_from_git_url(self) -> None:
        """Fill the blank derived fields from the git URL — create mode only (editing an
        existing repo leaves its values untouched). Only touches fields the user hasn't filled,
        so it's safe to run repeatedly (on blur and at submit)."""
        if self._editing:
            return
        repo = _repo_name_from_git_url(self.query_one("#field-git_url", Input).value)
        if not repo:
            return
        for field, derive in self._DERIVED.items():
            widget = self.query_one(f"#field-{field}", Input)
            if not widget.value.strip():
                widget.value = derive(repo)

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if event.widget.id == "field-git_url":
            self._autofill_from_git_url()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        self._autofill_from_git_url()  # backstop: fill blanks even if git_url never blurred
        values: dict[str, Any] = {}
        if not self._editing:
            values["id"] = self.query_one("#field-id", Input).value.strip()
        for name in self.FIELDS:
            values[name] = self.query_one(f"#field-{name}", Input).value.strip()
        values["docker_in_docker"] = self.query_one("#field-docker_in_docker", Checkbox).value
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReposScreen(ModalScreen[None]):
    """Repo management: list repos, create (`n`) / edit (`e`) them; Escape returns to the task
    view. Mutations go through the task service over REST, then the table refreshes."""

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
        table.add_columns("id", "name", "git_url", "default_base", "priv")
        table.focus()
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#repos", DataTable)
        table.clear()
        self._repos = {str(r["id"]): r for r in self._client.list_repos()}
        for repo in self._repos.values():
            priv = "✓" if (repo.get("capabilities") or {}).get("docker_in_docker") else "–"
            table.add_row(
                repo["id"], repo["name"], repo["git_url"], repo["default_base"], priv,
                key=str(repo["id"]),
            )
        self._current = self._current if self._current in self._repos else next(iter(self._repos), None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._current = str(key) if key is not None else None

    def action_close(self) -> None:
        self.dismiss(None)

    def action_new_repo(self) -> None:
        def create(values: dict[str, Any] | None) -> None:
            if values is None:  # backed out
                return
            if not (values["id"] and values["name"] and values["git_url"]):
                self.notify("id, name and git_url are required.", severity="warning")
                return
            try:
                self._client.create_repo(
                    values["id"], values["name"], values["git_url"], values["default_base"] or "main",
                    env_file=values["env_file"] or None, creds_volume=values["creds_volume"] or None,
                    capabilities={"docker_in_docker": values["docker_in_docker"]},
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

        def save(values: dict[str, Any] | None) -> None:
            if values is None:
                return
            # PATCH the core fields; image_layer_file is left intact. The privileged toggle is merged
            # onto the repo's existing capabilities so other keys (if any) survive.
            capabilities = {
                **self._repos[repo_id].get("capabilities", {}),
                "docker_in_docker": values["docker_in_docker"],
            }
            try:
                self._client.update_repo(
                    repo_id, name=values["name"], git_url=values["git_url"],
                    default_base=values["default_base"] or "main",
                    env_file=values["env_file"] or None, creds_volume=values["creds_volume"] or None,
                    capabilities=capabilities,
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


class ArtifactScreen(_OptionListModal[tuple[str, str]]):
    """A modal list of a task's artifacts: Enter opens the highlighted one over REST, `e` opens
    its local on-disk file in place; Escape cancels.

    Dismisses ``(name, mode)`` where ``mode`` is ``"rest"`` (Enter) or ``"local"`` (`e`), or
    ``None`` on cancel. Local-open is bound to `e` (as in "edit in place"), **not** Shift+Enter:
    many terminals can't deliver Shift+Enter distinctly from Enter, so the local mode would be
    silently unreachable."""

    CSS = """
    ArtifactScreen { align: center middle; }
    #artifact-box { width: 56; height: auto; max-height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    #artifact-hint { color: $text-muted; }
    """
    BOX_ID = "artifact-box"
    BINDINGS = [("escape", "cancel", "Cancel"), ("e", "open_local", "Open local")]

    def _extra_widgets(self) -> Iterable[Widget]:
        yield Label("enter: open · e: open local file · esc: cancel", id="artifact-hint")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss((str(event.option.prompt), "rest"))

    def action_open_local(self) -> None:
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        if index is None:
            return
        self.dismiss((str(option_list.get_option_at_index(index).prompt), "local"))


# The full keymap, single source of truth for **both** the footer legend and the help screen
# (`?`). Each ``Hotkey`` carries everything the two consumers need — the Textual key name, the
# action, the short footer label, the long help description, whether it shows in the footer, and an
# optional key display — so ``Dashboard.BINDINGS`` and ``HelpScreen`` are *derived* from this one
# tuple rather than repeating it. Ordered most-common first: the footer renders the ``show=True``
# subset (``t n x / d ? q``) in this order, and the help screen lists every key in it.
@dataclass(frozen=True)
class Hotkey:
    key: str  # the Textual key name ("t", "question_mark", "escape")
    action: str  # the bound ``action_*`` method ("attach", "help", "clear_search")
    label: str  # the short footer-legend label ("Attach")
    description: str  # the long help-screen description
    show: bool = True  # visible in the footer legend? (hidden keys still dispatch)
    display: str | None = None  # key_display override ("?" for question_mark, "Esc" for escape)

    def binding(self) -> Binding:
        """The Textual ``Binding`` this hotkey contributes to ``Dashboard.BINDINGS``.

        ``key_display`` is left ``None`` (Textual's default — render the key itself) unless this
        hotkey overrides it (``?`` for ``question_mark``, ``Esc`` for ``escape``)."""
        return Binding(self.key, self.action, self.label, show=self.show, key_display=self.display)


HOTKEYS: tuple[Hotkey, ...] = (
    Hotkey("t", "attach", "Attach", "Attach to the task's container tmux session"),
    Hotkey("n", "new_task", "New task", "New task (pick repo → workflow → describe)"),
    Hotkey("x", "drop", "Drop", "Drop the highlighted task"),
    Hotkey("/", "search", "Search", "Search tasks as you type"),
    Hotkey("d", "toggle_detail", "Detail", "Show/hide the detail pane"),
    Hotkey("r", "refresh", "Refresh", "Refresh from the task service now", show=False),
    Hotkey("R", "respawn", "Respawn", "Respawn a down task (release its claim)", show=False),
    Hotkey("p", "open_url", "Open URL", "Open the task's URL in the browser", show=False),
    Hotkey("g", "repos", "Repos", "Repo config (list / create / edit repos)", show=False),
    Hotkey("a", "artifacts", "Artifacts", "List the task's artifacts", show=False),
    Hotkey("s", "service", "Service", "Switch to the task-service session", show=False),
    Hotkey("u", "runner", "Runner", "Switch to the session-service (runner) session", show=False),
    Hotkey("y", "copy_slug", "Copy slug", "Copy the task's slug to the clipboard", show=False),
    Hotkey("Y", "copy_id", "Copy id", "Copy the task's id to the clipboard", show=False),
    Hotkey(
        "escape", "clear_search", "Clear search", "Clear the search filter",
        show=False, display="Esc",
    ),
    Hotkey("question_mark", "help", "Help", "This help screen", display="?"),
    Hotkey("q", "quit", "Quit", "Quit"),
)


class HelpScreen(ModalScreen[None]):
    """A modal listing **every** hotkey — the footer shows only the essential few, so this is
    the full keymap. Escape / `?` / `q` close it."""

    CSS = """
    HelpScreen { align: center middle; }
    #help-box { width: 64; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    #help-keys { padding-top: 1; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("question_mark", "close", "Close"),
        ("q", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        rows = "\n".join(
            f"  [b]{(h.display or h.key):<5}[/b] {h.description}" for h in HOTKEYS
        )
        with Vertical(id="help-box"):
            yield Label("panopticon — keys")
            yield Static(rows, id="help-keys")

    def action_close(self) -> None:
        self.dismiss(None)


class Dashboard(App[None]):
    """The task view. On `t` it calls ``on_switch`` with the task's session (and `s`/`u` call
    ``on_service``/``on_runner`` for the task-service / session-service runner sessions) and stays
    running; the supervisor handles the attach/detach (ADR 0009)."""

    CSS = (
        "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; display: none; } "
        "#search { display: none; }"
    )
    # The change-feed long-poll's ``wait`` ceiling: the feed worker parks each request up to this
    # many seconds before re-polling, so a quiet feed reconnects this often (no redraw) while a
    # change still returns — and redraws — immediately. It also bounds how long quitting waits on
    # the parked worker thread. 0/None disables the worker (manual `r` only; `make dashboard` one-shot).
    REFRESH_INTERVAL = 2.0
    # Only the essential, most-used keys show in the footer legend; the rest still dispatch but
    # are hidden (``show=False``) to keep the legend uncluttered — `?` opens HelpScreen, which
    # lists every key. Both the footer bindings and the help screen derive from the single
    # ``HOTKEYS`` table, so a key can't drift between the two.
    BINDINGS = [hotkey.binding() for hotkey in HOTKEYS]
    TITLE = "panopticon"

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        on_switch: Callable[[str], None] | None = None,
        on_service: Callable[[], bool] | None = None,
        on_runner: Callable[[], bool] | None = None,
        artifacts_root: str | Path = DEFAULT_ARTIFACTS,
        refresh_interval: float | None = REFRESH_INTERVAL,
    ) -> None:
        super().__init__()
        self._client = client
        self._on_switch = on_switch  # supervisor hook: record the pick + detach (None standalone)
        self._on_service = on_service  # `s` hook: switch to the service session; True if one exists
        self._on_runner = on_runner  # `u` hook: switch to the runner session; True if one exists
        self._artifacts_root = artifacts_root  # for `a`'s `e` local-open (co-located store)
        self._refresh_interval = refresh_interval  # change-feed long-poll wait (0/None → manual only)
        self._version = 0  # the change-feed cursor (X-Tasks-Version) the worker long-polls against
        self._tasks: dict[str, JsonObj] = {}
        self._current: str | None = None
        self._query: str = ""  # active search filter ("" → no filter); see action_search
        self._detail_visible = False  # detail pane hidden by default; `d` toggles it (action_toggle_detail)
        self._last_cursor_row = 0  # previous cursor row index → infer travel direction to skip the divider
        # one reused scratch dir for `a`'s REST-open (lazily made, cleaned on exit) — so opening
        # many artifacts doesn't leak a temp dir each.
        self._artifact_tmp: tempfile.TemporaryDirectory[str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="tasks")
            yield Static(id="detail")
        yield Input(id="search", placeholder="search tasks…")  # hidden until `/` (CSS display:none)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.cursor_type = "row"
        # the slug header carries a literal "[" — pass it as Text so Textual doesn't eat it as markup
        table.add_columns("state", "turn", "container", Text("slug[memo]"))
        table.focus()  # the (hidden) search Input would otherwise grab initial focus
        self.action_refresh()  # first paint; the feed worker drives every refresh after
        if not self._has_repos():  # first-run nudge: no repos → drop straight into the repo screen
            self.action_repos()
        if self._refresh_interval:
            self._watch_feed()

    def _has_repos(self) -> bool:
        """Whether the service reports any repos. On a fetch error, answer ``True`` (don't pop the
        repo screen on a down service — it couldn't list repos either; leave the operator on the
        task view, as the feed worker tolerates a not-yet-up service)."""
        try:
            return bool(self._client.list_repos())
        except Exception:
            return True

    @work(thread=True, exclusive=True, group="task-feed")
    def _watch_feed(self) -> None:
        """Redraw the table when tasks change, driven off the task service's change feed.

        Replaces the old fixed-interval timer: each iteration long-polls ``list_tasks_versioned``,
        which parks server-side until a task changes past our cursor (``_version``) or
        ``_refresh_interval`` seconds elapse. A change returns immediately, so the table redraws
        within a round-trip; a quiet feed just re-polls without redrawing. Runs in a thread (the
        REST client is blocking) — ``call_from_thread`` marshals the rebuild onto the UI thread,
        and Textual cancels the worker on unmount (the cancel lands when the parked poll returns)."""
        worker = get_current_worker()
        # Seed the cursor without redrawing: on_mount already painted the current snapshot, so the
        # first poll should wait for the *next* change rather than re-firing on the current version.
        try:
            _, self._version = self._client.list_tasks_versioned()
        except Exception:  # service not up yet — fall through; the loop retries with back-off
            pass
        while not worker.is_cancelled:
            try:
                _, version = self._client.list_tasks_versioned(
                    since=self._version, wait=self._refresh_interval
                )
            except Exception:  # transient feed error (service restart / blip) — back off, retry
                time.sleep(min(self._refresh_interval or 1.0, 1.0))
                continue
            if worker.is_cancelled:
                return
            if version != self._version:  # a task changed past our cursor → rebuild the table
                self._version = version
                try:
                    self.call_from_thread(self.action_refresh)
                except RuntimeError:  # app shut down between the cancel check and the dispatch
                    return

    def on_unmount(self) -> None:
        if self._artifact_tmp is not None:  # remove the REST-open scratch dir on exit
            self._artifact_tmp.cleanup()
            self._artifact_tmp = None

    def _artifact_tmpdir(self) -> str:
        """The app's reused scratch dir for REST-opened artifacts, created on first use."""
        if self._artifact_tmp is None:
            self._artifact_tmp = tempfile.TemporaryDirectory(prefix="panopticon-artifacts-")
        return self._artifact_tmp.name

    def action_refresh(self) -> None:
        table = self.query_one("#tasks", DataTable)
        selected = self._current  # keep the operator's highlight across the rebuild (feed refresh)
        table.clear()
        ordered = sorted(self._client.list_tasks(), key=_sort_key)  # live/user/recent first
        visible = [t for t in ordered if _matches(t, self._query)]  # apply the search filter
        self._tasks = {t["id"]: t for t in visible}
        # Draw the active↔terminal divider once, before the first terminal row — but only when an
        # active row precedes it (an all-terminal list gets no divider).
        seen_active = False
        separated = False
        for task in visible:
            terminal = task["state"] in TERMINAL_LABELS
            if terminal and seen_active and not separated:
                table.add_row(*_separator_cells(len(table.ordered_columns)), key=_SEPARATOR_KEY)
                separated = True
            seen_active = seen_active or not terminal
            table.add_row(
                task["state"], _turn_cell(task), _status_cell(task), _slug_cell(task),
                key=task["id"],
            )
        target = selected if selected in self._tasks else next(iter(self._tasks), None)
        if target is not None:
            table.move_cursor(row=table.get_row_index(target))
        self._update_detail(target)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#tasks", DataTable)
        if event.row_key.value == _SEPARATOR_KEY:
            # The arrow keys jump the divider: step one more row in the direction of travel
            # (down → first terminal task, up → last active task). The divider always sits
            # between groups, so there's a real row on both sides; move_cursor re-fires this
            # handler on that real row, so we don't recurse on the sentinel.
            step = 1 if table.cursor_row >= self._last_cursor_row else -1
            table.move_cursor(row=table.cursor_row + step)
            return
        self._last_cursor_row = table.cursor_row
        key = event.row_key.value
        self._update_detail(str(key) if key is not None else None)

    def _update_detail(self, task_id: str | None) -> None:
        if task_id == _SEPARATOR_KEY:  # the divider isn't a task — select nothing, blank the pane
            self._current = None
            self.query_one("#detail", Static).update("")
            return
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

                def create(memo: str | None) -> None:
                    if memo is None:  # backed out of the prompt
                        return
                    self._client.create_task(repo, workflow, memo.strip() or None)
                    self.action_refresh()

                self.push_screen(InputScreen("memo"), create)

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
        if task.get("container_status") == "live":
            self.notify("Container is live; drop it or let it finish.", severity="warning")
            return
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

    def _copy_to_clipboard(self, text: str) -> None:
        """Copy ``text`` to the clipboard two ways, best-effort: an OSC 52 emit (Textual's
        ``copy_to_clipboard`` — terminal-forwarded, so it survives tmux/ssh and needs no external
        tool) **and** the host's clipboard binary (`pbcopy`/`wl-copy`/`xclip`/`xsel`). Either path
        alone covers a gap the other has, and neither failure is allowed to crash the TUI."""
        try:
            self.copy_to_clipboard(text)  # OSC 52 — no-op on terminals that don't support it
        except Exception:  # never let a clipboard write take down the dashboard
            pass
        _clipboard_copy(text)  # host tool; best-effort (False when none installed)

    def action_copy_slug(self) -> None:
        """`y`: copy the highlighted task's slug to the clipboard (the human label, e.g. for the
        `panopticon/<slug>` branch). Warns when the task has no slug yet (unprovisioned)."""
        if self._current is None:
            return
        task = self._tasks.get(self._current)
        slug = task.get("slug") if task else None
        if not slug:
            self.notify("No slug set for this task.", severity="warning")
            return
        self._copy_to_clipboard(slug)
        self.notify(f"copied slug: {slug}")

    def action_copy_id(self) -> None:
        """`Y`: copy the highlighted task's id to the clipboard (the internal identifier)."""
        if self._current is None:
            return
        self._copy_to_clipboard(self._current)
        self.notify(f"copied id: {self._current}")

    def action_toggle_detail(self) -> None:
        """`d`: show/hide the right-hand detail pane. It starts hidden (``display: none``) so the
        task table — the only remaining row child — takes the full width; pressing `d` reveals the
        pane (with the current task's detail already rendered), and `d` again hides it."""
        self._detail_visible = not self._detail_visible
        self.query_one("#detail", Static).styles.display = (
            "block" if self._detail_visible else "none"
        )

    def action_help(self) -> None:
        """`?`: open the help screen — the full keymap (the footer shows only the essentials)."""
        self.push_screen(HelpScreen())

    def action_repos(self) -> None:
        """`g`: open the repo config screen — list repos, create/edit them (ADR 0002)."""
        self.push_screen(ReposScreen(self._client))

    def action_artifacts(self) -> None:
        """`a`: open a modal listing the highlighted task's artifacts. Enter opens the selection
        with the host's default handler by fetching it over REST to a temp file; `e` opens the
        on-disk file in place when the dashboard shares the artifact store (else warns).

        Opens on the machine running the dashboard, like `p`."""
        if self._current is None:
            return
        task_id = self._current
        try:
            names = self._client.list_artifacts(task_id)
        except httpx.HTTPStatusError as exc:
            self.notify(f"Can't list artifacts: {exc}", severity="error")
            return
        if not names:
            self.notify("No artifacts for this task.", severity="warning")
            return

        def open_selected(choice: tuple[str, str] | None) -> None:
            if choice is None:  # cancelled
                return
            name, mode = choice
            try:
                if mode == "local":  # open the on-disk file in place (co-located store)
                    path = FilesystemArtifactStore(self._artifacts_root).path(task_id, name)
                    if path is None:
                        self.notify(f"{name} isn't available locally.", severity="warning")
                        return
                    _open_path(str(path))
                    self.notify(f"opened {path} locally")
                else:  # "rest": fetch over REST to the scratch dir, then open
                    _open_via_rest(self._client, task_id, name, self._artifact_tmpdir())
                    self.notify(f"opened {name}")
            except FileNotFoundError:  # no opener binary on this host — notify, don't crash the TUI
                self.notify(f"No '{_open_command()}' on this host to open files.", severity="warning")
            except httpx.HTTPStatusError as exc:
                self.notify(f"Can't open {name}: {exc}", severity="error")

        self.push_screen(ArtifactScreen("artifacts", names), open_selected)

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

    def action_runner(self) -> None:
        """`u`: switch to the session-service (runner) tmux session, when one is running (ADR 0009).

        The runner is a sibling tmux session under `panopticon console`; ``on_runner`` switches
        to it the same way `s` switches to the service (record + detach), returning whether a runner
        session existed. Standalone (no supervisor) there is nothing to switch to."""
        if self._on_runner is None:
            self.notify("Runner shortcut is available when run via `panopticon console`.", severity="warning")
            return
        if not self._on_runner():
            self.notify("No session-service (runner) session is running.", severity="warning")

    def action_search(self) -> None:
        """`/`: enter search-as-you-type — reveal the query box and focus it (cloude-cade's `/`).

        Seeds the box with the active query so re-entering an existing filter is editable. While
        the box has focus it captures typed keys; the table filters live via ``on_input_changed``."""
        search = self.query_one("#search", Input)
        search.styles.display = "block"
        search.value = self._query
        search.focus()

    def action_clear_search(self) -> None:
        """`Esc`: clear the search filter and hide the box, whether typing or locked.

        A no-op clear when there's no active filter (just hides the box + returns focus to the
        table), so a stray `Esc` is harmless."""
        if self._query:
            self._query = ""
            self.action_refresh()
        self._hide_search()

    def _hide_search(self) -> None:
        """Hide the query box and return focus to the task table (Enter-lock and Esc-clear)."""
        self.query_one("#search", Input).styles.display = "none"
        self.query_one("#tasks", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the operator types in the search box (other Inputs are untouched)."""
        if event.input.id != "search":
            return
        self._query = event.value
        self.action_refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """`Enter` in the search box **locks** the filter: hide the box, keep the query, restore
        navigation. The `n`-flow's modal `InputScreen` handles its own submit, so this only fires
        for the dashboard's own search box."""
        if event.input.id != "search":
            return
        self._hide_search()


def run(
    client: TaskServiceClient,
    *,
    on_switch: Callable[[str], None] | None = None,
    on_service: Callable[[], bool] | None = None,
    on_runner: Callable[[], bool] | None = None,
    artifacts_root: str | Path = DEFAULT_ARTIFACTS,
) -> None:
    """Run the dashboard. ``on_switch``/``on_service``/``on_runner`` are the supervisor's `t`/`s`/`u`
    hooks (ADR 0009); all ``None`` standalone. ``artifacts_root`` is the local artifact-store root
    `a`'s `e` opens files from when the dashboard shares the task service's filesystem."""
    Dashboard(
        client, on_switch=on_switch, on_service=on_service, on_runner=on_runner,
        artifacts_root=artifacts_root,
    ).run()
