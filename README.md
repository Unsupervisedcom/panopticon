# panopticon

Orchestrate multiple coding agents across isolated tasks and **configurable workflows**.

A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.

## Getting started

### Prerequisites

| Tool | Linux | macOS |
|------|-------|-------|
| Docker Engine | distro package or [docker.com](https://docs.docker.com/engine/install/) | [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/) (**required** — provides `host.docker.internal`) |
| tmux | `apt-get install --yes tmux` | `brew install tmux` |
| Python 3.11+ | `apt-get install --yes python3` | `brew install python` or pyenv |
| uv | `pip install uv` | `brew install uv` |

> **macOS:** Docker Engine alone is not sufficient — Docker Desktop is required.
> See [docs/macos-setup.md](docs/macos-setup.md) for macOS-specific details and known limitations.

### 1. Clone and sync

```sh
git clone https://github.com/Unsupervisedcom/panopticon
cd panopticon
make sync
```

### 2. Mint an auth token

Task containers authenticate to Claude via `CLAUDE_CODE_OAUTH_TOKEN`. Mint one with:

```sh
claude setup-token
```

Create a `0600`-permission env-file and note its path — you'll wire it to a repo in step 5:

```sh
mkdir -p ~/.config/panopticon
echo "CLAUDE_CODE_OAUTH_TOKEN=<paste token here>" > ~/.config/panopticon/myrepo.env
chmod 600 ~/.config/panopticon/myrepo.env
```

### 3. Bootstrap

```sh
make bootstrap
```

This builds the base container image (streaming progress — no silent pause), runs the DB
migration, and opens the dashboard. The first build takes a few minutes; subsequent runs
skip it.

### 4. Try the demo (optional)

In a second terminal, while the dashboard is running:

```sh
panopticon demo
```

This registers a throwaway local repo and creates two spike tasks — no GitHub account or
token needed. Switch back to the dashboard to watch ≥ 2 agents working at once.

You can also run `make demo` from the project root.

### 5. Wire your own repo

From the dashboard, press `g` to add a repo (provide the git URL and the path to your
env-file from step 2), then press `n` to create a task.

## Architecture in one paragraph

A deterministic control plane (the **task service**) owns task state and drives
per-workflow state machines; a per-machine **runner** spawns task containers and host
tmux sessions; a **terminal controller** runs the dashboard. **All LLM calls happen
inside task containers** — the control plane, runner, and dashboard never call a model.
