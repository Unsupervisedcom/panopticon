# panopticon

Orchestrate many coding agents across isolated tasks and **configurable workflows** — and watch
them all from one dashboard.

A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade) prototype. The
full design lives on the [`design-docs`](../../tree/design-docs) branch (goals, parity analysis,
architecture, roadmap, and ADRs); [`CLAUDE.md`](CLAUDE.md) is the operating manual.

The terminal dashboard — every task, whose turn it is (agent working vs. waiting on you), and a
keystroke to jump into any agent's session:

```text
 panopticon
 state          turn    container  tokens   slug[memo]
 PLANNING       agent   live       1.2M     auth-setup-token[switch container auth to a long-lived setup-token]
 ITERATING      user    live       14.7M    token-estimate-field[add a token-estimate field to tasks]
 ORCHESTRATING  agent   starting   479K     replace-polling[replace polling loops with blocking requests]
 ─────────────  ──────  ─────────  ───────  ──────────────
 COMPLETE       user    down       7.6M     hide-detail-pane[hide the detail pane on start]
 DROPPED        user    down       2.9M     liveness-research[liveness chain-of-custody research]
```

## The one invariant

**All LLM calls happen inside task containers.** The control plane — task service, session
service, dashboard — is deterministic and never calls a model. That keeps the orchestration
predictable, testable, and cheap; the agent is the only thing that thinks.

## What's in the box

**Workflow** — the sequence of steps to complete a task. A workflow:
- defines the deliverable (a PR, a plan, a piece of research);
- defines the agent's responsibilities at each step;
- defines when the agent may act independently and when it must hand back for feedback.

**Container** — the agent's sandbox. It:
- isolates agents so many can work in parallel without stepping on each other;
- lets agents (more) safely escalate privileges — e.g. run with permissions skipped — because the
  blast radius is one task's throwaway checkout.

**Task service** — the task store for all repos. It:
- tracks every task and its current state (the deterministic state machine);
- doubles as the out-of-container artifact store (plans, notes);
- is reachable by tasks over **MCP**, so agents can share artifacts (one agent plans, another
  implements) or even spawn other tasks.

**Dashboard** — the user interface. From it you:
- see all work in progress at a glance;
- tell which agents are running independently and which are waiting on you;
- jump into an agent's session to give feedback, then drop back out.

**Session service** — manages the tmux sessions. It:
- provisions a tmux session per agent container;
- runs on a dedicated socket, so it never pollutes your own tmux sessions.

## Modular by design

The pieces are contracts, not a monolith:
- **Workflows are extensible** — define your own lifecycle.
- **Containers are customizable** — add a repo-specific image layer to set up your toolchain.
- **The task service, dashboard, and session service are swappable** — each defines a contract, so
  you could drop in, say, a web dashboard in place of the terminal one.

## Running it

Prerequisites: [`uv`](https://docs.astral.sh/uv/), Docker, and tmux.

```sh
make sync                 # create the venv and install deps
make build                # build the base task-container image
make login REPO=<id>      # populate a repo's credentials (one-time)
make panopticon           # bring up the task service + runner + dashboard
```

`make panopticon` starts everything on a dedicated tmux server and drops you into the dashboard:
create a task, the runner spawns its container, the agent works, and you join in with `t` when it
wants feedback. `make panopticon-down` stops it all.

## Create your first task

With the dashboard up (`make panopticon`):

1. **Add a repo.** Press `g` to open the repo screen and register a repository (its git URL and the
   workflow image layer). The dashboard opens here automatically when you have no repos yet.
2. **Create a task.** Press `n`, then pick the repo → a workflow → and type a short **memo**
   describing the work. The task appears in `PLANNING`.
3. **Let it spin up.** The runner claims the task, spawns its container, and the agent plans the work
   and names itself (the `slug`). The `container` column goes `starting → live`.
4. **Watch the turn.** The `turn` column tells you who holds the ball — `agent` means it's working;
   `user` means it finished a step and wants your feedback before going on.
5. **Jump in.** Highlight the task and press `t` to attach to the agent's session. Give feedback,
   then detach with `Ctrl-b d` to land right back on the same dashboard.
6. **Move it along.** The agent advances through the workflow's steps as you approve them; press `x`
   to drop a task or `R` to respawn a `down` one. Press `?` any time for the full keymap.

## Development

```sh
make check       # typecheck (mypy --strict) + tests — what CI runs
make test        # uv run pytest
make typecheck   # uv run mypy --package panopticon
```

See [`CLAUDE.md`](CLAUDE.md) for the module map, conventions, and the full set of `make` targets.
