# panopticon

**Agents write the code, you own what ships.**

That's easy with one agent. Run a fleet of them and it breaks down: the fleet stalls
waiting on you, and you lose track of which agent is doing what. Panopticon gives you
one place to watch them all.

- **A live dashboard** of all your tasks, showing which agents are working and which are blocked
  waiting on you, so you stop cycling through terminals to find the one that's stuck.
- **[Configurable workflows](docs/workflows/README.md)** that set the line between what an
  agent may do alone and what needs your sign-off, so agents run unattended without running
  unchecked. Other tools show you which agent is blocked; Panopticon decides when it blocks.
- **Sandboxed by default:** each agent works in its own container on its own branch
  (secrets and environment handled per repo), so it can work freely and nothing reaches
  main without your review.

Self-hosted and terminal-native: your infrastructure, your secrets,
your repos. A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.

New here? [`docs/overview.md`](docs/overview.md) explains how the pieces fit together: the
mental model behind the dashboard.

## The dashboard

The whole fleet in one terminal view — every task's `state`, whose `turn` it is (agent or you),
its `container` status, and its repo and slug:

```text
══════════════════════════════════════════════════════════════════════════
  panopticon                                                6 tasks
──────────────────────────────────────────────────────────────────────────
  state          turn       container   repo       slug[memo]
  ITERATING      agent      live        web-api    add-oauth[Add OAuth login]
  PLANNING       user       live        web-api    fix-upload[Flaky S3 upload]
  MERGING        agent      starting    dashboard  dark-mode[Dark-mode theme]
  ITERATING      user ⚠     down        web-api    migrate-db[Move to Postgres]
  ORCHESTRATING  agent      live        infra      q3-cleanup[Q3 tech-debt]
  PLANNING       agent      live        infra      └─ drop-py38[Drop Python 3.8]
  COMPLETE       agent      –           web-api    ship-readme[README refresh]
──────────────────────────────────────────────────────────────────────────
  t attach   n new task   x drop   / search   d detail   ? help   q quit
══════════════════════════════════════════════════════════════════════════
```

The `turn` column is color-coded live — green when the agent is working, yellow when it's your
move, red (`⚠`) when a task is blocked waiting on you — so you can tell at a glance which agents
need you. The `container` column tracks each agent's sandbox as it spawns (`queued → … → live`,
or `down` when one needs a respawn), and governed sub-tasks nest under their governor (`└─`).
Press `t` to drop into any task's session, `?` for the full key list.

## Requirements

Panopticon runs the control plane on your host and each agent in its own container, so it
shells out to a few host tools. You need:

- **Python 3.11+**
- **Docker**, with the daemon running
- **tmux:** the dashboard, console supervisor, and task sessions run on a dedicated
  `tmux -L panopticon` server
- **git:** the session service clones a per-task workspace for each agent
- The **`claude` CLI:** first-time setup runs `claude setup-token` on the host to mint the
  Claude auth token each agent uses inside its container

`panopticon quickstart` checks these first; run `panopticon doctor` to re-check any time.

## Install

Panopticon is a command-line app, so [pipx](https://pipx.pypa.io) is the recommended way to
install it: it puts the `panopticon` command on your `PATH` in its own isolated environment.
Plain `pip` works too.

```sh
# recommended: isolated, on your PATH
pipx install panopticon-app

# or with pip
pip install panopticon-app
```

The PyPI distribution is **`panopticon-app`**, but the command you run and the package you
import are both **`panopticon`**.

## Quickstart

Run `panopticon quickstart` **from inside the repo you want agents to work on**: it registers
whatever repo you're in as the target for your tasks.

```sh
cd ~/code/my-project   # the repo you want agents to work on
panopticon quickstart  # first-time setup, then open the dashboard
```

`panopticon quickstart` checks your prerequisites, brings the stack up, registers the repo
you're in, and drops you into a `setup-repo` task; it will walk you through minting a
repo-specific Claude token (saved to the repo's env-file). Then you create tasks and watch your
fleet from the dashboard.

## Your first task

On the dashboard:

1. **Create it.** Press `n`, then pick the repo and a workflow: `github-peer-reviewed` (opens a PR
   to merge) or `local-git-self-reviewed` (stays on local git, no GitHub needed). Describe
   the work in a sentence or two. See [`docs/workflows/`](docs/workflows/README.md) for the full
   catalog and how to choose.
2. **Watch it start.** The task's `container` column moves `queued → … → live` as the runner
   spawns its container; once it's `live` the agent starts on its own branch and begins planning
   automatically. Press `a` to open its plan when it's ready.
3. **Respond when it needs you.** The `turn` column shows whether the agent is working or waiting
   on you. When it wants a decision, like signing off on that plan, press `t` to attach to its
   session and steer it; run **`/advance`** there to approve its plan or to advance to the next
   stage in the workflow from whatever stage you're in. Detach any session with `Ctrl-b d` (or
   your own `tmux` prefix + `d`) to return to the dashboard.
4. **Review what ships.** For `github-peer-reviewed` the agent opens a PR (press `p` on the
   dashboard to open it in your browser); for `local-git-self-reviewed` it commits to the task
   branch for you to diff locally. Either way nothing lands until you `/advance` it: you own what
   ships.

## Configuration

Panopticon stores its data under standard XDG locations, each overridable by an environment
variable (resolution is `$PANOPTICON_*` → `$XDG_*_HOME/panopticon` → the default below):

| What | Default location | Override |
|---|---|---|
| Database | `~/.local/share/panopticon/panopticon.db` | `PANOPTICON_DB` (or `PANOPTICON_DATA`) |
| Artifacts + per-task clones | `~/.local/share/panopticon/` | `PANOPTICON_DATA` |
| Layers, secrets, workflows | `~/.config/panopticon/` | `PANOPTICON_CONFIG` (workflows also via the `--workflows-path` flag) |
| Per-repo clone cache | `~/.cache/panopticon/repos/` | `PANOPTICON_CACHE` |
