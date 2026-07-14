# panopticon

**Agents write the code, you own what ships.**

That's easy with one agent. Run a fleet of them and it breaks down: the fleet stalls
waiting on you, and you lose track of which agent is doing what. Panopticon gives you
one place to watch them all.

- **A live dashboard** of all your tasks — which agents are working, and which are blocked
  waiting on you — so you stop cycling through terminals to find the one that's stuck.
- **Configurable workflows** that set the line between what an agent may do alone and what
  needs your sign-off — so agents run unattended without running unchecked. Other tools show
  you which agent is blocked; Panopticon decides when it blocks.
- **Sandboxed by default** — each agent works in its own container on its own branch
  (secrets and environment handled per repo), so it can work freely and nothing reaches
  main without your review.

Self-hosted and terminal-native — your infrastructure, your secrets,
your repos. A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.

## Requirements

Panopticon runs the control plane on your host and each agent in its own container, so it
shells out to a few host tools. You need:

- **Python 3.11+**
- **Docker**, with the daemon running — each agent works in its own container
- **tmux** — the dashboard, console supervisor, and task sessions run on a dedicated
  `tmux -L panopticon` server
- **git** — the session service clones a per-task workspace for each agent
- The **`claude` CLI** — first-time setup runs `claude setup-token` on the host to mint the
  Claude auth token each agent uses inside its container

After installing (below), run `panopticon doctor` to check all of these at once — it prints a
`✓`/`✗` line per prerequisite and exits non-zero if anything is missing. On macOS, see
[`docs/macos-setup.md`](docs/macos-setup.md) for host setup notes.

## Install

Panopticon is a command-line app, so [pipx](https://pipx.pypa.io) is the recommended way to
install it — it puts the `panopticon` command on your `PATH` in its own isolated environment.
Plain `pip` works too.

```sh
# recommended — isolated, on your PATH
pipx install panopticon-app

# or with pip
pip install panopticon-app
```

The PyPI distribution is **`panopticon-app`**, but the command you run and the package you
import are both **`panopticon`**.

## Quickstart

Run `panopticon quickstart` **from inside the repo you want agents to work on** — it registers
whatever repo you're in as the target for your tasks. Just kicking the tires? Run it anywhere
outside a git checkout and it falls back to registering the panopticon repo itself, so you have
something to try it against.

```sh
cd ~/code/my-project   # the repo you want agents to work on
panopticon doctor      # verify host prerequisites (git, docker, tmux, claude)
panopticon quickstart  # first-time setup, then open the dashboard
```

`panopticon quickstart` is the one-command first-time setup. It applies the database schema
(creating `~/.local/share/panopticon/panopticon.db`), starts the task service, runner, and
dashboard supervisor on the `tmux -L panopticon` server, registers the repo you run it in, and
opens the console **already attached to a `setup-repo` task** — where you run `claude
setup-token` to mint your Claude auth token (it's written into the repo's env-file). From the
dashboard you create tasks and watch your fleet; `panopticon stop` tears everything down (task
containers and the `-L panopticon` server).

Prefer to drive the pieces yourself? The individual subcommands are `panopticon migrate`,
`build`, `start`, `stop`, `console`, `dashboard`, `tasks`, and `host` — but `quickstart` is the
path most people want.

## Configuration

Panopticon stores its data under standard XDG locations, each overridable by an environment
variable (resolution is `$PANOPTICON_*` → `$XDG_*_HOME/panopticon` → the default below):

| What | Default location | Override |
|---|---|---|
| Database | `~/.local/share/panopticon/panopticon.db` | `PANOPTICON_DB` (or `PANOPTICON_DATA`) |
| Artifacts + per-task clones | `~/.local/share/panopticon/` | `PANOPTICON_DATA` |
| Layers, secrets, workflows | `~/.config/panopticon/` | `PANOPTICON_CONFIG`, `--workflows-path` |
| Per-repo clone cache | `~/.cache/panopticon/repos/` | `PANOPTICON_CACHE` |

For the container auth token in detail, see [`docs/container-auth.md`](docs/container-auth.md).
The full design — goals, architecture, roadmap, and ADRs — lives under
[`docs/design/`](docs/design/).
