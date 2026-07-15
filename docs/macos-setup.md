# macOS setup

Install and first run are the same on macOS as anywhere else — see the
[README](../README.md) (`pipx install panopticon-app`, then `panopticon quickstart`). This page
covers only what's **macOS-specific**.

## Use Docker Desktop, not Docker Engine

Task containers reach the host task service via `host.docker.internal`, which **Docker Desktop for
Mac** injects automatically. Bare Docker Engine doesn't provide it, so tasks can't call home —
Docker Desktop is required. Install it from
[docs.docker.com/desktop](https://docs.docker.com/desktop/install/mac-install/), start it, and
confirm the daemon is up:

```sh
docker info
```

`panopticon doctor` checks this (along with tmux, git, the `claude` CLI, and Python), and
`panopticon quickstart` runs it for you before doing anything.

## What runs where

The control plane, dashboard, and `tmux -L panopticon` server run natively on the macOS host; each
task's agent runs inside the Docker Desktop Linux VM.

```
macOS host                          Docker Desktop Linux VM
──────────────────────────────      ──────────────────────────────────
task service                        panopticon-<id> containers
session-service runner                └─ agent (claude CLI)
dashboard                             └─ /workspace (per-task clone)
tmux -L panopticon server             └─ entrypoint.sh (Linux tools)
```

`docker/Dockerfile` and `docker/entrypoint.sh` use Linux-only commands (`groupmod`, `useradd`,
`gosu`, …) — intentional; they always run inside the Linux VM, never on the host.

## Known limitations on macOS

- **`--network host`** isn't supported by Docker Desktop for Mac. Panopticon doesn't use it —
  containers reach the host via `host.docker.internal`.
- **Docker-in-Docker** (`capabilities.docker_in_docker`) uses `--privileged`, which Docker Desktop
  supports. On Apple Silicon, if the task image is `linux/amd64`-only, disable "Use Rosetta for
  x86/amd64 emulation" in Docker Desktop settings or rebuild for `arm64`.
- **tmux must be installed** before you start Panopticon — if it's missing, session launches fail
  silently. `panopticon doctor` catches this.

## Developing from source

Contributing rather than just running it? The `make` targets work on macOS with the same Docker
Desktop + tmux requirements above — add `uv` (`brew install uv`), then `make sync`, `make build`,
`make start`. `make stop` (or `panopticon stop`) tears everything down.
