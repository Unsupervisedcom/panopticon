"""The Textual dashboard (ADR 0002 presentation adapter): the operator's view of tasks.

A task table on the left, the highlighted task's state/turn/history on the right. It refreshes
from the task service **on change** — a background worker long-polls the change feed
(``list_tasks_versioned``), so the table redraws within a round-trip of a state change and stays
still when nothing changes (no fixed-interval redraw); `r` forces a refresh now. The redraw
preserves the highlighted row across the rebuild. Terminal (COMPLETE/DROPPED) tasks sink below
active ones and are rendered in faded/dim styling so they recede visually without a hard separator.

The task table, the repo table (`g`), and the `OptionList` pickers (`n`'s repo/workflow choice,
`a`'s artifact list) all accept vim-style `h`/`j`/`k`/`l` as well as the arrow keys.

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

`Enter` on a **governing task** (one with governed children) **collapses** its sub-tasks into a
single dim placeholder row (its slug cell renders ``...``); pressing `Enter` again **expands** them.
Arrow keys skip the ensemble row (it is not a real task). Expanding or collapsing does not affect the task service — it is pure
display state local to the dashboard.

The `container` column shows each task's container status: `live` (an active registration), `down`
(was up, container gone — respawn with `R`), `starting` (claimed, no registration yet — its
container is still coming up), `healing` (the runner is self-healing an orphan), or `–` (unclaimed
or just released by `R`, awaiting the runner's re-claim). Liveness is the registration, independent
of provisioning.

The dashboard does not attach to tmux itself: on `t` it calls ``on_switch`` (the terminal
supervisor, ADR 0009 §6, records the chosen session and detaches this client) and **keeps
running**, so when the supervisor re-attaches after the operator detaches the task, it is the
same live dashboard — cursor and all. Network calls are synchronous (small, local); moving them
to Textual workers is a refinement (docs/design/BACKLOG.md).
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import shlex
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
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets._select import NoSelection as _SelectNoSelection
from textual.worker import get_current_worker

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.dirs import ARTIFACTS_DIR
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.local_runner import session_name
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.terminal.setup_repo_task import create_setup_repo_task


def _make_sort_key(
    by_updated: bool = False,
) -> Callable[[JsonObj], tuple[bool, bool, float, str]]:
    """Return a sort key function for the task table.

    1. non-terminal before terminal — COMPLETE/DROPPED sink to the bottom.
    2. turn priority: for active tasks the user's turn comes first (operator action needed);
       for terminal tasks the agent's turn comes first (task just finished).
    3. timestamp:
       - Active, ``by_updated=False`` (default): ``created_at`` descending — newest first
         (stable: ``created_at`` never changes, so rows don't reorder when a task updates).
       - Active, ``by_updated=True``: ``updated_at`` descending — most recently updated rises first.
       - Terminal (always): ``updated_at`` descending — most recently completed rises first.
    4. id as a stable tiebreaker.
    """

    def key(task: JsonObj) -> tuple[bool, bool, float, str]:
        is_terminal = task["state"] in TERMINAL_LABELS
        turn_first = "agent" if is_terminal else "user"
        turn_after_priority = task["turn"] != turn_first  # False (priority) sorts before True
        if is_terminal or by_updated:
            # Terminal tasks always use updated_at descending; by_updated mode does too.
            raw = task.get("updated_at") or ""
            try:
                ts = -datetime.fromisoformat(raw).timestamp()  # negative → newest first
            except ValueError:
                ts = 0.0
        else:
            raw = task.get("created_at") or task.get("updated_at") or ""
            try:
                ts = -datetime.fromisoformat(raw).timestamp()  # negative → newest first
            except ValueError:
                ts = 0.0
        return (
            is_terminal,  # False (active) before True (terminal)
            turn_after_priority,  # priority turn sorts first within each section
            ts,
            task["id"],  # stable tiebreaker
        )

    return key


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


# Row-key prefix for ensemble placeholder rows. When the operator collapses a governing task
# (Enter on a governor), its governed children are replaced by one dim placeholder row (its slug
# cell renders ``...``) whose key is ``f"{_ENSEMBLE_KEY_PREFIX}{governor_id}"``. Keyboard navigation skips these rows
# (_VimDataTable steps the cursor straight past them) and ``on_data_table_row_selected`` ignores them.
_ENSEMBLE_KEY_PREFIX = "__ensemble__"


def _dim(cell: Text | str) -> Text:
    """Return a dim copy of a cell value (str or Rich Text), fading it without erasing content."""
    t = Text(cell if isinstance(cell, str) else cell.plain)
    t.stylize("dim")
    return t


def _slug_cell(task: JsonObj, prefix: str = "") -> Text:
    """The ``slug[memo]`` column: the slug followed by the task's memo in brackets.

    Bare slug when there's no memo; bare ``[memo]`` (no leading dash) when there's a
    memo but no slug; ``-`` only when neither is set.

    ``prefix`` is a tree-connector string (e.g. ``"├─ "``, ``"│  └─ "``) prepended
    for governed tasks to show their relationship to the governor visually.  It is
    rendered dim so it doesn't compete with the task name.

    Returned as a Rich ``Text`` (like :func:`_turn_cell`), **not** a markup string: Textual renders
    bare ``str`` cells through console markup, which swallows the ``[…]`` — so a plain string would
    show just the bare slug (the bug that hid memos; the header had the same problem)."""
    slug = task.get("slug") or ""
    memo = task.get("memo")
    text = Text()
    if prefix:
        text.append(prefix, style="dim")
    if memo:
        first_line = memo.splitlines()[0] if memo else memo
        text.append(f"{slug}[{first_line}]")
    else:
        text.append(slug or "-")
    return text


def _group_section(
    tasks: list[JsonObj],
    collapsed: frozenset[str] | set[str] = frozenset(),
) -> list[tuple[JsonObj, str]]:
    """Group governed tasks under their governor within a single section (active or terminal).

    Takes a pre-sorted list of tasks from one section and returns ``(task, prefix)`` pairs.
    ``prefix`` is the tree-connector string to render before the task's slug (e.g. ``"├─ "``
    for a non-last child, ``"└─ "`` for the last child).  Tasks whose governor is absent
    from the list — or root/ungoverned tasks — get an empty prefix.

    Nested trees are supported: a governed task that itself governs others gets the
    appropriate continuation bars in its children's prefixes (``"│  "`` if more siblings
    follow, ``"   "`` after the last sibling).

    ``collapsed`` is the set of governor task IDs whose ensembles are currently collapsed.
    A collapsed governor's children are replaced by a single synthetic ``ensemble`` row
    (a dict with ``"_ensemble": True``) so the caller can render a dim placeholder instead
    of the full sub-tree."""
    task_ids = {t["id"] for t in tasks}
    children: dict[str, list[JsonObj]] = {}
    for t in tasks:
        gov = t.get("governor_task_id")
        if gov and gov in task_ids:
            children.setdefault(gov, []).append(t)

    governed_ids = {t["id"] for bucket in children.values() for t in bucket}

    def expand(task: JsonObj, prefix: str, child_continuation: str) -> list[tuple[JsonObj, str]]:
        task_children = children.get(task["id"], [])
        result: list[tuple[JsonObj, str]] = [(task, prefix)]
        if task["id"] in collapsed and task_children:
            # Collapsed: replace all children with a single ensemble placeholder row.
            ensemble: JsonObj = {
                "_ensemble": True,
                "_governor_id": task["id"],
                "_count": len(task_children),
            }
            result.append((ensemble, child_continuation + "└─ "))
        else:
            for i, child in enumerate(task_children):
                is_last = i == len(task_children) - 1
                connector = "└─ " if is_last else "├─ "
                grandchild_cont = child_continuation + ("   " if is_last else "│  ")
                result.extend(expand(child, child_continuation + connector, grandchild_cont))
        return result

    result: list[tuple[JsonObj, str]] = []
    for task in tasks:
        if task["id"] not in governed_ids:
            result.extend(expand(task, "", ""))
    return result


def _group_by_governor(
    tasks: list[JsonObj],
    collapsed: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[tuple[JsonObj, str]], list[tuple[JsonObj, str]]]:
    """Reorder tasks so governed tasks appear immediately after their governor.

    Section (active vs terminal) is determined by the governor chain, not just the task's
    own state: a task is "active" for placement purposes if it *or any ancestor governor*
    is non-terminal. This keeps governed tasks nested under their governor in the active
    section even when the governed task itself has reached a terminal state.

    Returns ``(active_section, terminal_section)`` as separate lists so the caller can
    insert the divider at the structural boundary rather than inspecting per-row state.

    ``collapsed`` is forwarded to :func:`_group_section`; see its docs for the ensemble
    collapse behaviour."""
    task_by_id = {t["id"]: t for t in tasks}

    def section_is_active(task_id: str, visited: set[str]) -> bool:
        if task_id not in task_by_id or task_id in visited:
            return False
        visited.add(task_id)
        task = task_by_id[task_id]
        if task["state"] not in TERMINAL_LABELS:
            return True
        gov_id = task.get("governor_task_id")
        return section_is_active(gov_id, visited) if gov_id else False

    active = [t for t in tasks if section_is_active(t["id"], set())]
    terminal = [t for t in tasks if not section_is_active(t["id"], set())]
    return _group_section(active, collapsed), _group_section(terminal, collapsed)


# Fields a search query matches against (cloude-cade filters on the task title; our nearest
# analogs are the task's identifying text). Joined and lowercased into one haystack per task.
# ``repo_name`` is injected into each task dict by ``action_refresh`` before this runs.
_SEARCH_FIELDS = ("slug", "state", "workflow", "memo", "repo_name")


def _matches(task: JsonObj, query: str) -> bool:
    """Case-insensitive substring match of ``query`` against a task's identifying fields.

    An empty query matches everything (no filter). Mirrors cloude-cade's "title contains the
    query" — plain substring, no fuzzy ranking.

    Ensemble placeholder rows (``"_ensemble": True``) always match: they're synthetic and
    only emitted when their governor is visible, so filtering them would orphan the connector."""
    if task.get("_ensemble"):
        return True
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

#: Container statuses whose tmux session exists and can be attached (`t`). ``live`` = an open
#: container registration (a docker task); ``awaiting`` = the session is up but not (yet) registered
#: — a docker task mid-boot, or a **shell** task, which runs no agent so never registers and sits at
#: ``awaiting`` for its whole run (its session *is* its liveness). Both name the session the same way
#: (``panopticon-<task_id>``, see :func:`session_name`), so attach keys off the status, not a
#: registration lookup — which is what lets `t` reach a shell task at all.
_ATTACHABLE_STATUSES = {"live", "awaiting"}


def _status_cell(task: JsonObj) -> Text:
    """The container column: the task service's composed ``container_status``, color-coded."""
    status = task.get("container_status") or "–"
    return Text(status, style=_STATUS_COLORS.get(status, "dim"))


def _repo_cell(task: JsonObj, repo_names: dict[str, str]) -> str:
    """The repo column: the repo's human-readable name, looked up from the id→name cache."""
    repo_id = str(task.get("repo_id") or "")
    return repo_names.get(repo_id, repo_id) if repo_id else "?"


def render_detail(task: JsonObj) -> str:
    """The right-pane text for one task: identity, state/turn, and history.

    **Plain text** — the caller wraps it in a Rich ``Text`` so it renders literally. We deliberately
    do *not* use console markup here: a field can contain a stray ``[`` (e.g. a docker command
    captured in ``lifecycle_detail`` — ``['--add-host', …]``, or a memo), and markup-parsing that
    string crashes the whole pane. (Rich's escaper + Textual's markup parser also disagree on which
    ``[`` is a tag, so escaping isn't reliable — rendering literally is.)"""
    turn = f"{task['turn']}{' (blocked)' if task.get('blocked') else ''}"
    claim = f"    claimed: {task['claimed_by']}" if task.get("claimed_by") else ""
    lines = [
        task.get("slug") or task["id"],
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
        lines += ["", f"tokens (wt): {used} used / {est} est"]
    lines += ["", "history:"]
    for entry in task.get("history") or []:
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


def _edit_with_editor(text: str) -> str:
    """Open ``text`` in ``$EDITOR`` (falling back to ``vi``) and return the saved content.

    Uses ``shlex.split`` on the editor value so multi-word settings like ``"code --wait"``
    or ``"vim -u NONE"`` work correctly."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        subprocess.run([*shlex.split(editor), path])
        return Path(path).read_text(encoding="utf-8")
    finally:
        os.unlink(path)


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


def _apply_memo_filter(memo: str) -> bool:
    """Return ``True`` if ``memo`` matched a filter and was handled, ``False`` to proceed normally."""
    if memo.upper() == __import__("base64").b64decode(b"RkFSVEJBUkY=").decode():
        with contextlib.suppress(FileNotFoundError):
            _open_path(
                __import__("base64")
                .b64decode(b"aHR0cHM6Ly93d3cueW91dHViZS5jb20vd2F0Y2g/dj1na3g5VmFMdkx6QQ==")
                .decode()
            )
        return True
    return False


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
            yield _VimOptionList(*self._options)
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


class WorkflowScreen(_OptionListModal[str]):
    """Workflow picker with a description area below the list.

    Shows each workflow's ``when_to_use`` text in a Static pane as the user moves through the list,
    so the operator can see what situation each workflow is designed for before committing."""

    CSS = """
    WorkflowScreen { align: center middle; }
    #workflow-choice-box { width: 64; height: auto; max-height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    #workflow-desc { height: 4; border: tall $panel; padding: 0 1; color: $text-muted; }
    """
    BOX_ID = "workflow-choice-box"

    def __init__(self, workflows: list[dict[str, str]]) -> None:
        names = [w["name"] for w in workflows]
        super().__init__("workflow", names)
        self._workflow_map = {w["name"]: w.get("when_to_use", "") for w in workflows}

    def _extra_widgets(self) -> Iterable[Widget]:
        yield Static("", id="workflow-desc")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        name = str(event.option.prompt)
        self.query_one("#workflow-desc", Static).update(self._workflow_map.get(name, ""))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))


class MemoTextArea(TextArea):
    """TextArea for memo input where Enter submits the form instead of inserting a newline.

    Starts one row tall and grows up to ``MAX_LINES`` rows as content is loaded (e.g. from
    ``ctrl+g`` / ``$EDITOR``). The user can't type newlines directly; the editor is the
    intended path for multi-line memos."""

    MAX_LINES = 10

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()  # don't let Enter bubble to the screen's enter binding
            self.screen.action_submit()  # type: ignore[attr-defined]
        else:
            await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        lines = max(1, len(self.text.splitlines()))
        self.styles.height = min(lines, self.MAX_LINES)


class MemoScreen(ModalScreen["tuple[str, bool] | None"]):
    """Memo + auto-submit checkbox for task creation.

    Dismisses ``(text, auto_submit)`` on submit (Enter), or ``None`` on cancel (Escape).
    ``auto_submit_default`` seeds the checkbox; the user can toggle it with Space.

    Uses :class:`MemoTextArea` so Enter submits rather than inserting a newline — same UX
    as the original single-line ``Input``, but the field can display multi-line content
    loaded by ``ctrl+g`` (open in ``$EDITOR``).

    **Space toggles the checkbox; Enter saves**."""

    CSS = """
    MemoScreen { align: center middle; }
    #memo-box { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #memo-box MemoTextArea { height: 1; margin-bottom: 1; }
    #memo-box Checkbox { margin-top: 0; }
    #memo-box .memo-hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+g", "edit_in_editor", "Edit"),
        ("enter", "submit", "Create"),
    ]

    def __init__(self, auto_submit_default: bool) -> None:
        super().__init__()
        self._auto_submit_default = auto_submit_default

    def compose(self) -> ComposeResult:
        with Vertical(id="memo-box"):
            yield Label("memo")
            yield MemoTextArea(compact=True)
            yield SpaceCheckbox("Submit as initial prompt", value=self._auto_submit_default)
            yield Label("ctrl+g: open in $EDITOR", classes="memo-hint")

    def on_mount(self) -> None:
        self.query_one(MemoTextArea).focus()

    def action_submit(self) -> None:
        text = self.query_one(MemoTextArea).text
        auto_submit = self.query_one(SpaceCheckbox).value
        self.dismiss((text, auto_submit))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_edit_in_editor(self) -> None:
        ta = self.query_one(MemoTextArea)
        try:
            with self.app.suspend():
                result = _edit_with_editor(ta.text)
        except SuspendNotSupported:
            self.app.notify("Editor not supported in this environment", severity="warning")
            return
        ta.load_text(result)
        ta.focus()


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
    return tail[: -len(".git")] if tail.endswith(".git") else tail


class SpaceCheckbox(Checkbox, inherit_bindings=False):
    """A :class:`Checkbox` that toggles on **Space only** (Textual's default binds ``enter,space``).
    Dropping Enter lets the key bubble up to the screen, so Enter saves the form even while the
    checkbox holds focus — the form's Space-toggles / Enter-saves contract. ``inherit_bindings=False``
    keeps the base ``enter,space`` toggle binding from being merged back in; ``ToggleButton`` is
    its only source, so re-declaring ``space`` is the whole keymap."""

    BINDINGS = [Binding("space", "toggle_button", "Toggle", show=False)]


class _VimDataTable(DataTable[Any]):
    """A :class:`DataTable` with vim-style ``hjkl`` layered onto the default arrow keys (default
    ``inherit_bindings=True``, so the arrow keys still work — this just adds a second way in).

    Vertical movement also **skips ensemble placeholder rows** (keys prefixed with
    ``_ENSEMBLE_KEY_PREFIX``): the cursor steps straight onto the next real row rather than
    landing on the sentinel and bouncing off it, so a collapsed ensemble is never briefly
    highlighted mid-traversal. Both the arrow keys and ``j``/``k`` route through
    ``action_cursor_down``/``action_cursor_up``, so overriding those covers every input path."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]

    def _move_skipping(self, direction: int) -> None:
        """Move the cursor one step in ``direction`` (+1 down, -1 up), stepping over any ensemble
        placeholder rows so it lands on the first real row. If only sentinels or the table edge
        lie beyond, stay put — never land on a sentinel."""
        rows = self.ordered_rows
        target = self.cursor_row + direction
        while 0 <= target < len(rows):
            key = rows[target].key.value
            if isinstance(key, str) and key.startswith(_ENSEMBLE_KEY_PREFIX):
                target += direction
                continue
            self.move_cursor(row=target)
            return

    def action_cursor_down(self) -> None:
        self._move_skipping(1)

    def action_cursor_up(self) -> None:
        self._move_skipping(-1)


class _VimOptionList(OptionList):
    """An :class:`OptionList` with vim-style ``j``/``k`` layered onto the default arrow keys —
    it's a single column, so there's no ``h``/``l`` equivalent."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


def _list_secrets_files() -> list[str]:
    """Return the sorted **names** (relative to the secrets dir) of files in the config secrets dir.

    An ``env_file`` is stored relative to the secrets dir so it resolves on whichever host runs the
    task (ADR 0007), so the picker offers bare names, not absolute paths."""
    from panopticon.core.dirs import _secrets_dir

    secrets_dir = _secrets_dir()
    if not secrets_dir.is_dir():
        return []
    return sorted(p.name for p in secrets_dir.iterdir() if p.is_file())


class EnvFileField(Widget):
    """Secrets env-file picker for the repo form.

    Shows a ``Select`` dropdown listing the file **names** found in the config secrets directory
    (``~/.config/panopticon/secrets/``), with an ``enter custom path…`` option at the bottom that
    reveals a free-form ``Input``. The stored value is always a **name relative to the secrets
    dir** (so it resolves on whichever host runs the task, ADR 0007); the custom input accepts an
    absolute or relative path and normalizes it to that relative name on read (see
    :func:`~panopticon.core.dirs.relativize_secrets_file`).
    """

    DEFAULT_CSS = """
    EnvFileField { margin-bottom: 1; height: auto; }
    EnvFileField #env-file-input { margin-top: 1; }
    """

    _CUSTOM = "__custom__"

    def __init__(self, initial: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self._initial = initial
        self._known = _list_secrets_files()

    def compose(self) -> ComposeResult:
        known_set = set(self._known)
        options: list[tuple[str, str]] = [(p, p) for p in self._known]
        options.append(("enter custom path…", self._CUSTOM))
        is_custom = bool(self._initial and self._initial not in known_set)
        yield Select(
            options,
            prompt="env_file (name in secrets dir or custom path)",
            allow_blank=True,
            value=self._initial if (self._initial and not is_custom) else Select.NULL,
            id="env-file-select",
        )
        inp = Input(
            value=self._initial if is_custom else "",
            placeholder="repo.env (or a path — normalized to a secrets-dir name)",
            id="env-file-input",
        )
        inp.display = is_custom
        yield inp

    @property
    def env_file_value(self) -> str:
        """The stored env_file **name** (relative to the secrets dir), or ``""`` when unset.

        A dropdown pick is already a bare name; a custom entry is normalized from whatever path the
        operator typed (absolute or relative) via
        :func:`~panopticon.core.dirs.relativize_secrets_file`."""
        from panopticon.core.dirs import relativize_secrets_file

        try:
            sel = self.query_one("#env-file-select", Select)
        except NoMatches:
            return ""
        v = sel.value
        if isinstance(v, _SelectNoSelection) or v == self._CUSTOM:
            try:
                return relativize_secrets_file(self.query_one("#env-file-input", Input).value)
            except NoMatches:
                return ""
        return str(v)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "env-file-select":
            return
        inp = self.query_one("#env-file-input", Input)
        if event.value == self._CUSTOM:
            inp.display = True
            inp.focus()
        else:
            inp.display = False


class RepoFormScreen(ModalScreen["dict[str, Any] | None"]):
    """A modal form for a repo's fields. On save (Enter or Ctrl+S) it hands the collected
    ``{field: value}`` dict to ``on_submit`` (which validates + persists); if that returns an
    error string the form shows it inline and **stays open** so invalid input isn't lost, and it
    dismisses (with the values dict) only on success. Escape cancels, dismissing ``None``. The
    text fields are strings; the privileged toggle is the bool ``docker_in_docker``.

    Two tabs: **general** (git URL, id, name, base branch, env file, privileged docker) and
    **workflows** (a per-workflow opt-in/opt-out checklist). Both tabs' values are collected
    on save — submitting from either tab captures everything.

    **Space toggles checkboxes; Enter saves the form** from any field. The :class:`SpaceCheckbox`
    subclass drops the default ``enter`` binding so Enter always bubbles up to the screen's save
    action rather than toggling.

    The **git URL leads** create mode: blank ``id`` and ``name`` auto-fill from it on blur and
    at submit; ``default_base`` defaults to ``main``. Edit mode leaves existing values untouched.
    Edit mode shows ``id`` read-only; ``image_layer_file`` and other capability keys aren't
    edited in the TUI (a PATCH update leaves them untouched)."""

    CSS = """
    RepoFormScreen { align: center middle; }
    #repo-form { width: 72; height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    #repo-form Input { margin-bottom: 1; }
    #repo-form Checkbox { margin-bottom: 1; }
    #form-tabs { height: 1fr; }
    #pane-workflows { height: 1fr; }
    #wf-scroll { height: 1fr; }
    #wf-scroll SpaceCheckbox { margin-bottom: 0; }
    #wf-desc { height: 4; border: tall $panel; padding: 0 1; color: $text-muted; margin-top: 1; }
    #form-error { color: $error; text-align: center; }
    #form-hint { color: $text-muted; text-align: center; margin-top: 1; }
    #pane-general EnvFileField #env-file-input { margin-bottom: 0; }
    """
    # Enter saves from any field. Text Inputs consume Enter via their own submit binding (posting
    # Input.Submitted → on_input_submitted), so this screen binding only fires for fields that
    # don't — SpaceCheckboxes and the read-only id Label — and never double-saves.
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "submit", "Save"),
        ("ctrl+s", "submit", "Save"),
    ]

    # git_url leads (the auto-fill source); the rest follow. ``id`` is rendered between git_url
    # and these, separately, since it's editable only in create mode. ``env_file`` is rendered
    # as an EnvFileField (dropdown + custom-path input) rather than a plain Input.
    FIELDS = ("git_url", "name", "default_base")
    # Fields auto-derived from git_url → how to derive each (create mode only; see
    # _autofill_from_git_url). id and name are the bare repo name.
    _DERIVED: dict[str, Callable[[str], str]] = {
        "id": lambda repo: repo,
        "name": lambda repo: repo,
    }

    def __init__(
        self,
        title: str,
        repo: JsonObj | None = None,
        workflows: list[dict[str, Any]] | None = None,
        on_submit: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._repo = repo or {}
        self._editing = repo is not None
        self._workflows = workflows or []
        self._wf_enabled: set[str] = set(self._repo.get("enabled_workflows") or [])
        self._wf_disabled: set[str] = set(self._repo.get("disabled_workflows") or [])
        # The parent supplies this: it attempts the submission (validation + REST) and returns an
        # error message to show inline (form stays open) or None on success (form dismisses). This
        # is what keeps an invalid form open instead of closing it and toasting the error after.
        self._on_submit = on_submit

    def _initial(self, name: str) -> str:
        """A field's pre-populated value: the repo's stored value, else (create mode only)
        ``main`` for ``default_base``, else blank."""
        stored = self._repo.get(name)
        if stored:
            return str(stored)
        return "main" if name == "default_base" and not self._editing else ""

    def _wf_checked(self, wf: dict[str, Any]) -> bool:
        name = wf["name"]
        if wf.get("opt_in"):
            return name in self._wf_enabled
        return name not in self._wf_disabled

    def compose(self) -> ComposeResult:
        with Vertical(id="repo-form"):
            yield Label(self._title)
            with TabbedContent(id="form-tabs"):
                with TabPane("general", id="pane-general"):
                    yield Input(
                        value=self._initial("git_url"), placeholder="git_url", id="field-git_url"
                    )
                    if self._editing:
                        yield Label(f"id: {self._repo['id']}")
                    else:
                        yield Input(placeholder="id", id="field-id")
                    for name in self.FIELDS[1:]:  # git_url already rendered above
                        yield Input(value=self._initial(name), placeholder=name, id=f"field-{name}")
                    yield EnvFileField(initial=self._initial("env_file"), id="field-env_file")
                    yield SpaceCheckbox(
                        "privileged docker (docker-in-docker)",
                        value=bool(self._repo.get("capabilities", {}).get("docker_in_docker")),
                        id="field-docker_in_docker",
                    )
                with TabPane("workflows", id="pane-workflows"):
                    if self._workflows:
                        with VerticalScroll(id="wf-scroll"):
                            for wf in self._workflows:
                                yield SpaceCheckbox(
                                    wf["name"], value=self._wf_checked(wf), id=f"wf-{wf['name']}"
                                )
                        yield Static("", id="wf-desc")
                    else:
                        yield Label("no workflows available")
            yield Static("", id="form-error")
            yield Static("enter: save   esc: cancel", id="form-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        if not isinstance(widget, SpaceCheckbox) or not (widget.id or "").startswith("wf-"):
            return
        name = (widget.id or "").removeprefix("wf-")
        desc = next((w.get("when_to_use", "") for w in self._workflows if w["name"] == name), "")
        with contextlib.suppress(NoMatches):
            self.query_one("#wf-desc", Static).update(desc)

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
        values["env_file"] = self.query_one("#field-env_file", EnvFileField).env_file_value or None
        values["docker_in_docker"] = self.query_one("#field-docker_in_docker", Checkbox).value
        enabled: list[str] = []
        disabled: list[str] = []
        for wf in self._workflows:
            name = wf["name"]
            try:
                checked = self.query_one(f"#wf-{name}", SpaceCheckbox).value
            except NoMatches:
                continue
            if wf.get("opt_in"):
                if checked:
                    enabled.append(name)
            else:
                if not checked:
                    disabled.append(name)
        values["enabled_workflows"] = enabled
        values["disabled_workflows"] = disabled
        # Let the parent attempt the submission while the modal is still open: an error message
        # is shown inline and the form stays put (so invalid input isn't lost); None means success.
        error = self._on_submit(values) if self._on_submit else None
        if error is not None:
            self.query_one("#form-error", Static).update(error)
            return
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
        ("s", "setup_repo", "Setup repo"),
        ("escape", "close", "Close"),
    ]

    def __init__(self, client: TaskServiceClient) -> None:
        super().__init__()
        self._client = client
        self._repos: dict[str, JsonObj] = {}
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="repos-box"):
            yield Label("repos — n: new   e: edit   s: setup   esc: close")
            yield _VimDataTable(id="repos")

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
                repo["id"],
                repo["name"],
                repo["git_url"],
                repo["default_base"],
                priv,
                key=str(repo["id"]),
            )
        self._current = (
            self._current if self._current in self._repos else next(iter(self._repos), None)
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._current = str(key) if key is not None else None

    def action_close(self) -> None:
        self.dismiss(None)

    def action_new_repo(self) -> None:
        # Returns an error to show inline (the form stays open, keeping the user's input) or None
        # on success. The form only closes when this returns None.
        def create(values: dict[str, Any]) -> str | None:
            if not (values["id"] and values["name"] and values["git_url"]):
                return "id, name and git_url are required."
            try:
                self._client.create_repo(
                    values["id"],
                    values["name"],
                    values["git_url"],
                    values["default_base"] or "main",
                    env_file=values["env_file"] or None,
                    capabilities={"docker_in_docker": values["docker_in_docker"]},
                    enabled_workflows=values["enabled_workflows"],
                    disabled_workflows=values["disabled_workflows"],
                )
            except httpx.HTTPStatusError as exc:
                return f"Can't create: {_detail(exc)}"
            self._refresh()
            return None

        workflows = self._client.list_workflows()
        self.app.push_screen(RepoFormScreen("new repo", workflows=workflows, on_submit=create))

    def action_edit_repo(self) -> None:
        if self._current is None:
            return
        repo_id = self._current

        # Returns an error to show inline (the form stays open) or None on success.
        def save(values: dict[str, Any]) -> str | None:
            # PATCH the core fields; image_layer_file is left intact. The privileged toggle is merged
            # onto the repo's existing capabilities so other keys (if any) survive.
            capabilities = {
                **self._repos[repo_id].get("capabilities", {}),
                "docker_in_docker": values["docker_in_docker"],
            }
            try:
                self._client.update_repo(
                    repo_id,
                    name=values["name"],
                    git_url=values["git_url"],
                    default_base=values["default_base"] or "main",
                    env_file=values["env_file"] or None,
                    capabilities=capabilities,
                    enabled_workflows=values["enabled_workflows"],
                    disabled_workflows=values["disabled_workflows"],
                )
            except httpx.HTTPStatusError as exc:
                return f"Can't update: {_detail(exc)}"
            self._refresh()
            return None

        workflows = self._client.list_workflows()
        self.app.push_screen(
            RepoFormScreen(
                f"edit {repo_id}", repo=self._repos[repo_id], workflows=workflows, on_submit=save
            )
        )

    def action_setup_repo(self) -> None:
        """`s`: run host-side setup for the highlighted repo — create a `setup-repo` task.

        The `setup-repo` workflow is hidden from the pickers, so this is how it's launched: one
        task, seeded with a memo, on the repo under the cursor."""
        if self._current is None:
            self.notify("Highlight a repo first.", severity="warning")
            return
        repo_id = self._current
        name = str(self._repos[repo_id].get("name", repo_id))
        try:
            create_setup_repo_task(self._client, repo_id, name)
        except httpx.HTTPStatusError as exc:
            self.notify(f"Can't create setup-repo task: {_detail(exc)}", severity="error")
            return
        self.notify(f"Created setup-repo task for {name}.")
        self.dismiss(None)  # back to the task view, where the new task shows up


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
    silently unreachable.

    Dotfile artifacts (names starting with ``.``) are hidden by default.  A "Show hidden"
    checkbox appears when hidden artifacts exist; toggling it repopulates the list."""

    CSS = """
    ArtifactScreen { align: center middle; }
    #artifact-box { width: 56; height: auto; max-height: 80%; padding: 1 2; border: round $accent; background: $surface; }
    #artifact-hint { color: $text-muted; }
    """
    BOX_ID = "artifact-box"
    BINDINGS = [("escape", "cancel", "Cancel"), ("e", "open_local", "Open local")]

    def __init__(self, title: str, all_names: list[str]) -> None:
        self._all_names = all_names
        visible = [n for n in all_names if not n.startswith(".")]
        super().__init__(title, visible)

    def _extra_widgets(self) -> Iterable[Widget]:
        yield Label("enter: open · e: open local file · esc: cancel", id="artifact-hint")
        if any(n.startswith(".") for n in self._all_names):
            yield SpaceCheckbox("Show hidden", id="show-hidden")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        names = (
            self._all_names
            if event.value
            else [n for n in self._all_names if not n.startswith(".")]
        )
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        for name in names:
            option_list.add_option(name)

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
    Hotkey("o", "toggle_sort", "Sort order", "Toggle sort: created ↔ updated", show=False),
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
        "escape",
        "clear_search",
        "Clear search",
        "Clear the search filter",
        show=False,
        display="Esc",
    ),
    Hotkey("question_mark", "help", "Help", "This help screen", display="?"),
    Hotkey("q", "quit", "Quit", "Quit"),
)


class _StatusFooter(Footer):
    """Footer extended with a task-counter Static docked to the right.

    Subclassing (rather than passing a child to Footer()) is necessary because Footer
    calls recompose() when ``_bindings_ready`` toggles, which clears and recreates its
    children. By yielding the counter inside compose() we ensure it survives rebuilds.
    ``_counter_text`` is persisted on the instance so each recompose restores the last
    value rather than resetting to empty (which would let the hint pills expand and cover
    the counter's space)."""

    _counter_text: str = ""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._counter_text, id="task-counter")

    def set_counter(self, text: str) -> None:
        self._counter_text = text
        for counter in self.query("#task-counter").results(Static):
            counter.update(text)


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
        # Enter and hjkl are handled on the list widgets themselves (not HOTKEYS bindings), so
        # they're listed here as literal lines rather than derived from the HOTKEYS table.
        enter_line = (
            f"  [b]{'Enter':<5}[/b] Collapse/expand the ensemble of governed tasks under the cursor"
        )
        vim_line = (
            f"  [b]{'hjkl':<5}[/b] Vim-style navigation (task/repo tables, option-list pickers)"
        )
        rows = "\n".join(f"  [b]{(h.display or h.key):<5}[/b] {h.description}" for h in HOTKEYS)
        with Vertical(id="help-box"):
            yield Label("panopticon — keys")
            yield Static(enter_line + "\n" + vim_line + "\n" + rows, id="help-keys")

    def action_close(self) -> None:
        self.dismiss(None)


def _setup_task_columns(table: DataTable[Any], *, multi_runner: bool) -> None:
    """Add the task table's columns. Includes a "runner" column when tasks span multiple hosts."""
    if multi_runner:
        table.add_columns("state", "turn", "container", "runner", "repo", Text("slug[memo]"))
    else:
        table.add_columns("state", "turn", "container", "repo", Text("slug[memo]"))


class Dashboard(App[None]):
    """The task view. On `t` it calls ``on_switch`` with the task's session (and `s`/`u` call
    ``on_service``/``on_runner`` for the task-service / session-service runner sessions) and stays
    running; the supervisor handles the attach/detach (ADR 0009)."""

    CSS = (
        "#tasks { width: 3fr; } #detail { width: 2fr; padding: 0 1; display: none; } "
        "#search { display: none; } "
        "#task-counter { dock: right; width: auto; padding: 0 1; }"
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
        on_switch: Callable[[str, str | None], None] | None = None,
        on_service: Callable[[], bool] | None = None,
        on_runner: Callable[[], bool] | None = None,
        artifacts_root: str | Path = ARTIFACTS_DIR,
        refresh_interval: float | None = REFRESH_INTERVAL,
    ) -> None:
        super().__init__()
        self._client = client
        self._on_switch = on_switch  # supervisor hook: record the pick + detach (None standalone)
        self._on_service = on_service  # `s` hook: switch to the service session; True if one exists
        self._on_runner = on_runner  # `u` hook: switch to the runner session; True if one exists
        self._artifacts_root = artifacts_root  # for `a`'s `e` local-open (co-located store)
        self._refresh_interval = (
            refresh_interval  # change-feed long-poll wait (0/None → manual only)
        )
        self._version = 0  # the change-feed cursor (X-Tasks-Version) the worker long-polls against
        self._tasks: dict[str, JsonObj] = {}
        self._repo_names: dict[str, str] = {}  # repo id → name; populated by _load_repo_names
        self._current: str | None = None
        self._query: str = ""  # active search filter ("" → no filter); see action_search
        self._detail_visible = (
            False  # detail pane hidden by default; `d` toggles it (action_toggle_detail)
        )
        self._collapsed: set[str] = set()  # governor IDs whose ensembles are currently collapsed
        self._first_refresh: bool = True  # seed _collapsed with all governors on first refresh
        self._governors: set[str] = set()  # governor IDs visible in the current table build
        self._multi_runner: bool = False  # True when tasks span >1 distinct runner_host
        self._sort_by_updated: bool = (
            False  # False = creation order (stable); True = updated_at (newest first)
        )
        # one reused scratch dir for `a`'s REST-open (lazily made, cleaned on exit) — so opening
        # many artifacts doesn't leak a temp dir each.
        self._artifact_tmp: tempfile.TemporaryDirectory[str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield _VimDataTable(id="tasks")
            yield Static(id="detail")
        yield Input(id="search", placeholder="search tasks…")  # hidden until `/` (CSS display:none)
        yield _StatusFooter()

    def _load_repo_names(self) -> None:
        """Refresh the repo id→name cache from the task service."""
        try:
            repos = self._client.list_repos()
            self._repo_names = {str(r["id"]): str(r["name"]) for r in repos}
        except Exception:
            pass

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.cursor_type = "row"
        _setup_task_columns(table, multi_runner=False)
        table.focus()  # the (hidden) search Input would otherwise grab initial focus
        self._load_repo_names()
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
        # service not up yet — fall through; the loop retries with back-off
        with contextlib.suppress(Exception):
            _, self._version = self._client.list_tasks_versioned()
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
        ordered = sorted(self._client.list_tasks(), key=_make_sort_key(self._sort_by_updated))
        new_multi_runner = (
            len({r.get("host") for r in self._client.live_runners() if r.get("host")}) > 1
        )
        if new_multi_runner != self._multi_runner:
            table.clear(columns=True)  # rows already gone; also clears columns for rebuild
            self._multi_runner = new_multi_runner
            _setup_task_columns(table, multi_runner=self._multi_runner)
        active = [t for t in ordered if t.get("state") not in TERMINAL_LABELS]
        agent_on = sum(1 for t in active if t.get("turn") == "agent")
        sort_label = "sort: updated" if self._sort_by_updated else "sort: created"
        self.query_one(_StatusFooter).set_counter(
            f"active agents {agent_on}/{len(active)}  ·  {sort_label}"
        )
        # Inject repo_name so _matches can search on it without a separate lookup per task.
        for task in ordered:
            task["repo_name"] = self._repo_names.get(str(task.get("repo_id") or ""), "")
        # Governor IDs: the set of task IDs that have at least one governed child in the full
        # snapshot. Computed from ``ordered`` (pre-collapse, pre-filter) so collapsing a governor
        # doesn't remove it from the set and prevent a second Enter from re-expanding it.
        self._governors = {t["governor_task_id"] for t in ordered if t.get("governor_task_id")}
        # Prune stale collapsed entries for governors no longer present (e.g. task deleted),
        # then seed all governors as collapsed on the very first refresh.
        self._collapsed &= self._governors
        if self._first_refresh:
            self._collapsed = set(self._governors)
            self._first_refresh = False
        # Group governed tasks under their governor (within each section), then filter.
        # The two sections come back separately so the divider sits at the structural
        # boundary — not based on individual task state (a terminal governed task can live
        # in the active section when its governor is still active).
        # While a search is active, expand every ensemble so collapsed children are
        # searchable — otherwise a `└─ ...` placeholder hides them from the filter. We
        # pass an empty collapsed set (not mutating self._collapsed), so the operator's
        # collapse state is restored as soon as the query is cleared.
        collapsed_for_display = set() if self._query else self._collapsed
        # When a query is active, filter *before* grouping and pull each matching task's
        # governor chain up with it: a visible child must keep its ancestors visible, or the
        # tree breaks (a child rendered under a governor that got filtered out is orphaned).
        # This is one-directional — a matching governor does not pull its children down.
        if self._query:
            task_by_id_all: dict[str, JsonObj] = {t["id"]: t for t in ordered}
            visible_ids: set[str] = set()
            for t in ordered:
                if _matches(t, self._query):
                    tid: str | None = str(t["id"])
                    while tid is not None and tid not in visible_ids:
                        visible_ids.add(tid)
                        parent = task_by_id_all.get(tid)
                        tid = parent.get("governor_task_id") if parent else None
            search_filtered = [t for t in ordered if t["id"] in visible_ids]
        else:
            search_filtered = ordered
        active_group, terminal_group = _group_by_governor(search_filtered, collapsed_for_display)
        active_visible = list(active_group)
        terminal_visible = list(terminal_group)
        visible = active_visible + terminal_visible
        # Build the task index (real tasks only; ensemble placeholders are synthetic).
        self._tasks = {t["id"]: t for t, _ in visible if not t.get("_ensemble")}

        def _add_row(task: JsonObj, prefix: str) -> None:
            if task.get("_ensemble"):
                gov_id = task["_governor_id"]
                slug_cell = Text(f"{prefix}...", style="dim")
                runner_blank = (Text(""),) if self._multi_runner else ()
                table.add_row(
                    Text(""),
                    Text(""),
                    Text(""),
                    *runner_blank,
                    Text(""),
                    slug_cell,
                    key=f"{_ENSEMBLE_KEY_PREFIX}{gov_id}",
                )
            else:
                state_cell: Text | str = task["state"]
                turn_cell = _turn_cell(task)
                status_cell = _status_cell(task)
                runner_cell: Text | None = (
                    Text(task.get("runner_host") or "") if self._multi_runner else None
                )
                repo_cell: Text | str = _repo_cell(task, self._repo_names)
                slug_cell_real = _slug_cell(task, prefix)
                if task["state"] in TERMINAL_LABELS:
                    state_cell = _dim(state_cell)
                    turn_cell = _dim(turn_cell)
                    status_cell = _dim(status_cell)
                    if runner_cell is not None:
                        runner_cell = _dim(runner_cell)
                    repo_cell = _dim(repo_cell)
                    slug_cell_real = _dim(slug_cell_real)
                runner_extra = (runner_cell,) if runner_cell is not None else ()
                table.add_row(
                    state_cell,
                    turn_cell,
                    status_cell,
                    *runner_extra,
                    repo_cell,
                    slug_cell_real,
                    key=task["id"],
                )

        for task, prefix in active_visible:
            _add_row(task, prefix)
        for task, prefix in terminal_visible:
            _add_row(task, prefix)
        target = selected if selected in self._tasks else next(iter(self._tasks), None)
        if target is not None:
            table.move_cursor(row=table.get_row_index(target))
        self._update_detail(target)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if isinstance(key, str) and key.startswith(_ENSEMBLE_KEY_PREFIX):
            # Keyboard navigation skips ensemble sentinels (see _VimDataTable), so this only
            # fires for a mouse hover/click onto the placeholder — leave the detail pane as-is
            # rather than trying to render a non-task.
            return
        self._update_detail(str(key) if key is not None else None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """`Enter` on a governing task collapses or expands its **ensemble** of governed children.

        Pressing Enter on a task that has governed children toggles its collapsed state: a
        collapsed governor's sub-tasks are replaced by a single dim ensemble placeholder row (its
        slug cell renders ``...``); pressing Enter again restores the full tree.  Enter on a
        non-governor (or on a sentinel row) is a no-op."""
        key = event.row_key.value
        if not isinstance(key, str):
            return
        if key.startswith(_ENSEMBLE_KEY_PREFIX):
            return
        if key not in self._governors:
            return
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self.action_refresh()

    def _update_detail(self, task_id: str | None) -> None:
        self._current = task_id
        if not self._detail_visible:
            return
        task: JsonObj | None = None
        if task_id:
            try:
                task = self._client.get_task(task_id)
            except Exception:
                task = self._tasks.get(
                    task_id
                )  # fall back to summary when the service is unreachable
        # wrap in Text so the pane renders literally — never parse task content as console markup
        # (a "[" in e.g. a docker-command lifecycle_detail would otherwise crash the whole dashboard)
        self.query_one("#detail", Static).update(
            Text(render_detail(task)) if task else Text("no tasks")
        )

    def action_new_task(self) -> None:
        """`n`: create a task — pick a repo, a workflow, describe the work, then POST it."""
        repos = [str(r["id"]) for r in self._client.list_repos()]
        if not repos:
            self.notify("Need at least one repo to create a task.", severity="warning")
            return

        def pick_workflow(repo: str | None) -> None:
            if repo is None:
                return
            workflows = self._client.list_workflows_for_repo(repo)
            if not workflows:
                self.notify(f"No workflows enabled for repo {repo!r}.", severity="warning")
                return

            def describe(workflow: str | None) -> None:
                if workflow is None:
                    return
                wf_info = next((w for w in workflows if w["name"] == workflow), {})
                auto_submit_default = bool(wf_info.get("auto_submit_memo", False))

                def create(result: tuple[str, bool] | None) -> None:
                    if result is None:  # backed out
                        return
                    memo_text, auto_submit = result
                    stripped = memo_text.strip()
                    if _apply_memo_filter(stripped):
                        return
                    if auto_submit and stripped:
                        self._client.create_task(repo, workflow, stripped, initial_prompt=stripped)
                    else:
                        self._client.create_task(repo, workflow, stripped or None)
                    self.action_refresh()

                self.push_screen(MemoScreen(auto_submit_default), create)

            self.push_screen(WorkflowScreen(workflows), describe)

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
        """`R`: kill any running container/session for this task and respawn it.

        Releases the claim so the host runner re-claims and re-spawns; the runner's ``spawn()``
        kills any existing tmux session and force-removes the container before starting fresh.
        Unclaimed tasks have nothing to respawn."""
        task_id = self._current
        if task_id is None:
            return
        task = self._tasks.get(task_id)
        if not task or not task.get("claimed_by"):
            self.notify("Task isn't claimed by a runner — nothing to respawn.", severity="warning")
            return
        self._client.release(task_id)  # back to unclaimed → the host runner kills + re-spawns
        self.notify("Respawning: the runner will stop and restart the container.")
        self.action_refresh()

    def action_attach(self) -> None:
        """`t`: hand off to the highlighted task's tmux session, if it's running.

        Calls ``on_switch`` (the supervisor records the session and detaches this client, then
        attaches the task) and **keeps running**, so returning lands on this same live dashboard
        (ADR 0009). Switching is always detach→attach, never `switch-client`. Standalone (no
        supervisor) there is nothing to attach to.

        Attachable when the composed ``container_status`` says a session exists
        (:data:`_ATTACHABLE_STATUSES`) — ``live`` for a registered container, ``awaiting`` for a
        session that's up but unregistered (a docker task mid-boot, or a **shell** task, which never
        registers). The session name is derived, not read from a registration
        (:func:`session_name`), so this reaches a shell task the same as a container one."""
        if self._current is None:
            return
        if self._on_switch is None:
            self.notify(
                "Attach is available when run via `panopticon console`.", severity="warning"
            )
            return
        task = self._tasks.get(self._current)
        status = task.get("container_status") if task else None
        if status not in _ATTACHABLE_STATUSES:
            self.notify("No running session for this task.", severity="warning")
            return
        runner_host = task.get("runner_host") if task else None
        self._on_switch(session_name(self._current), runner_host)

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
        # never let a clipboard write take down the dashboard
        with contextlib.suppress(Exception):
            self.copy_to_clipboard(text)  # OSC 52 — no-op on terminals that don't support it
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

    def action_toggle_sort(self) -> None:
        """`o`: toggle between sorting by creation time or update time."""
        self._sort_by_updated = not self._sort_by_updated
        self.action_refresh()

    def action_toggle_detail(self) -> None:
        """`d`: show/hide the right-hand detail pane. It starts hidden (``display: none``) so the
        task table — the only remaining row child — takes the full width; pressing `d` reveals the
        pane (with the current task's detail already rendered), and `d` again hides it."""
        self._detail_visible = not self._detail_visible
        self.query_one("#detail", Static).styles.display = (
            "block" if self._detail_visible else "none"
        )
        if self._detail_visible:
            self._update_detail(self._current)

    def action_help(self) -> None:
        """`?`: open the help screen — the full keymap (the footer shows only the essentials)."""
        self.push_screen(HelpScreen())

    def action_repos(self) -> None:
        """`g`: open the repo config screen — list repos, create/edit them (ADR 0002)."""

        def _on_repos_dismissed(_: None) -> None:
            self._load_repo_names()  # pick up any renames/additions before the table rebuilds
            self.action_refresh()

        self.push_screen(ReposScreen(self._client), _on_repos_dismissed)

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
                self.notify(
                    f"No '{_open_command()}' on this host to open files.", severity="warning"
                )
            except httpx.HTTPStatusError as exc:
                self.notify(f"Can't open {name}: {exc}", severity="error")

        self.push_screen(ArtifactScreen("artifacts", names), open_selected)

    def action_service(self) -> None:
        """`s`: switch to the task-service tmux session, when one is running (ADR 0009).

        The service is a sibling tmux session under `panopticon console`; ``on_service`` switches
        to it the same way `t` switches to a task (record + detach), returning whether a service
        session existed. Standalone (no supervisor) there is nothing to switch to."""
        if self._on_service is None:
            self.notify(
                "Service shortcut is available when run via `panopticon console`.",
                severity="warning",
            )
            return
        if not self._on_service():
            self.notify("No task-service session is running.", severity="warning")

    def action_runner(self) -> None:
        """`u`: switch to the session-service (runner) tmux session, when one is running (ADR 0009).

        The runner is a sibling tmux session under `panopticon console`; ``on_runner`` switches
        to it the same way `s` switches to the service (record + detach), returning whether a runner
        session existed. Standalone (no supervisor) there is nothing to switch to."""
        if self._on_runner is None:
            self.notify(
                "Runner shortcut is available when run via `panopticon console`.",
                severity="warning",
            )
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
        navigation."""
        if event.input.id != "search":
            return
        self._hide_search()


def run(
    client: TaskServiceClient,
    *,
    on_switch: Callable[[str, str | None], None] | None = None,
    on_service: Callable[[], bool] | None = None,
    on_runner: Callable[[], bool] | None = None,
    artifacts_root: str | Path = ARTIFACTS_DIR,
) -> None:
    """Run the dashboard. ``on_switch``/``on_service``/``on_runner`` are the supervisor's `t`/`s`/`u`
    hooks (ADR 0009); all ``None`` standalone. ``artifacts_root`` is the local artifact-store root
    `a`'s `e` opens files from when the dashboard shares the task service's filesystem."""
    Dashboard(
        client,
        on_switch=on_switch,
        on_service=on_service,
        on_runner=on_runner,
        artifacts_root=artifacts_root,
    ).run()
