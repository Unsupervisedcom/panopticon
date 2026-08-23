# Dashboard keybindings

Every key the terminal dashboard responds to, grouped by context. The footer legend shows only
the essential few; press `?` in the dashboard for the full list on demand. This page is that list,
kept in sync with the `HOTKEYS` keymap and the modal `BINDINGS` in
[`src/panopticon/terminal/dashboard.py`](../src/panopticon/terminal/dashboard.py).

## Task list (the main view)

| Key | Action |
| --- | --- |
| `t` | Attach to the task's container tmux session |
| `n` | New task (pick repo, then workflow, then describe the work) |
| `x` | Drop the highlighted task |
| `d` | Show the task's detail in a modal |
| `o` | Toggle sort order: created ↔ updated |
| `r` | Refresh from the task service now |
| `R` | Respawn a down task (releases its claim so the runner re-spawns it) |
| `p` | Open the task's URL in the browser |
| `e` | Snooze the highlighted task for 12 hours |
| `E` | Snooze the highlighted task indefinitely |
| `a` | List the task's artifacts |
| `g` | Open the repo config screen |
| `s` | Switch to the task-service session |
| `u` | Switch to the session-service (runner) session |
| `y` | Copy the task's slug to the clipboard |
| `Y` | Copy the task's id to the clipboard |
| `?` | Help — a modal listing every key |
| `q` | Quit |

`x` (drop) is the only state transition the dashboard drives; every other transition starts a new
agentic turn and is triggered from inside the container.

## Navigation

| Key | Action |
| --- | --- |
| `↑` `↓` / `k` `j` | Move the cursor up / down |
| `h` `l` | Vim-style navigation, alongside the arrow keys |

Vim keys work in the task table, the repo table, and the option-list pickers (the `n` repo/workflow
choice, the `a` artifact list).

## Search

| Key | Action |
| --- | --- |
| `/` | Search tasks as you type (matches slug, state, workflow, memo) |
| `Enter` | Lock the filter — hide the box, keep the filter, restore navigation |
| `Esc` | Clear the filter (from the search box or a locked filter) |

## Ensembles

| Key | Action |
| --- | --- |
| `Enter` | Collapse/expand the governed tasks under a governing task |

`Enter` on a governing task (one with governed children) toggles its sub-tasks between a single dim
placeholder row and the full list. This is display-only — it does not touch the task service.

## Task detail modal (`d`)

| Key | Action |
| --- | --- |
| `Esc` / `d` / `q` | Close |

## Artifacts modal (`a`)

| Key | Action |
| --- | --- |
| `Enter` | Open the selected artifact with the host's default handler |
| `e` | Open the on-disk file in place (when the dashboard shares the artifact store) |
| `Ctrl-a` | Attach new local files to the task |
| `Esc` | Cancel |

## New-task memo (`n`)

| Key | Action |
| --- | --- |
| `Enter` | Submit the memo as the agent's initial prompt |
| `Ctrl-s` | Set the memo without submitting it (an unsent paste) |
| `Ctrl-g` | Edit the memo in `$EDITOR` |
| `Ctrl-a` | Attach local files as the new task's artifacts |
| `Esc` | Cancel |

## Attach-files modal (`Ctrl-a`)

Reached from the new-task memo or the artifacts modal.

| Key | Action |
| --- | --- |
| `Enter` (path field) | Add the typed file to the queue |
| `Enter` (on a queued file) | Remove it from the queue |
| `Esc` | Done — return with the queued files |

## Repos modal (`g`)

| Key | Action |
| --- | --- |
| `n` | New repo |
| `e` | Edit the highlighted repo |
| `s` | Setup repo |
| `Esc` | Close |

## Supervisor (attached to a task)

| Key | Action |
| --- | --- |
| `Ctrl-b d` | Detach and return to the dashboard |

After `t` attaches you to a task's session, the terminal session supervisor brings you back to the
dashboard when you detach with `Ctrl-b d` (or your own `tmux` prefix + `d`).
