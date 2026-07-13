# macOS developer setup

This guide covers what you need to run panopticon on macOS.

## Prerequisites

| Tool | How to install | Why |
|------|----------------|-----|
| Docker Desktop for Mac | [docs.docker.com/desktop/install/mac-install/](https://docs.docker.com/desktop/install/mac-install/) | Provides the Linux VM that runs task containers; injects `host.docker.internal` automatically |
| tmux | `brew install tmux` | `make start` and `make stop` use `tmux -L panopticon` on the macOS host |
| Python 3.11+ | `brew install python` or pyenv | The control plane runs natively on macOS |
| uv | `brew install uv` or `pip install uv` | Dependency manager (`make sync`, `make test`, etc.) |

Docker Engine alone (without Docker Desktop) is **not sufficient** on macOS — `host.docker.internal` would not be available.

## What runs where

```
macOS host                          Docker Desktop Linux VM
──────────────────────────────      ──────────────────────────────────
make start  → task service          panopticon-<id> containers
             + session service         └─ agent (claude CLI)
             + dashboard              └─ /workspace (per-task clone)
tmux -L panopticon server            └─ entrypoint.sh (Linux tools)
```

`docker/Dockerfile` and `docker/entrypoint.sh` use Linux-only commands (`groupmod`, `useradd`,
`gosu`, etc.) — this is correct and intentional; they always run inside the Linux VM.

## Quick start

```sh
# 1. Install prerequisites
brew install tmux uv

# 2. Install Docker Desktop, start it, and verify
docker info

# 3. Install Python dependencies
make sync

# 4. Build the base task-container image
make build

# 5. Bring everything up
make start
```

## Verifying prerequisites

Run `panopticon doctor` to check that the host has everything the `quickstart`, `start` and
`setup-repo` flows need — git, docker (and a reachable daemon), tmux, the `claude` CLI, and
Python 3.11+. It prints a line per check and exits non-zero if any are missing, so a fresh pip
install (`pip install panopticon-app`) can self-diagnose before the first `panopticon
quickstart`.

`make start` launches the task service and session-service runner as background tmux sessions on
the `panopticon` tmux server, then opens the dashboard supervisor in the foreground.

## Stopping

```sh
make stop   # kills task containers and the -L panopticon tmux server
```

## Known limitations on macOS

- **`--network host`** is not supported by Docker Desktop for Mac. Panopticon does not use it —
  containers reach the host task service via `host.docker.internal`, which Docker Desktop injects
  automatically.
- **Docker-in-Docker** (`capabilities.docker_in_docker`) uses `--privileged`, which Docker Desktop
  supports. On Apple Silicon, if the task image is `linux/amd64`-only, disable "Use Rosetta for
  x86/amd64 emulation" in Docker Desktop settings or rebuild for `arm64`.
- **tmux must be installed** before `make start`. If it is missing, the session launches will
  silently fail.
