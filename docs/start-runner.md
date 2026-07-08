# `panopticon start-runner` — start a session-service runner

`panopticon start-runner` starts a `panopticon.sessionservice.host` runner,
either locally (`--local`) or on a remote machine via SSH.

```
panopticon start-runner --local          # start a runner on this machine
panopticon start-runner <host>           # SSH to <host> and start a runner there
```

## Overview

The command gives operators a single entry point for all runner lifecycle.
`--local` replaces the direct `python -m panopticon.sessionservice.host` call
(used by `make start`) and ensures the runner registers with no hostname, so
locally-claimed tasks attach without triggering an unnecessary SSH hop.

The remote form (no `--local`) solves a different problem: compute on another
machine.  A reverse port forward means the remote machine only needs a normal
outbound SSH connection — no inbound access to the task service required.

## Modes

### Local mode (`--local`)

```
panopticon start-runner --local
```

Starts the session service on the current machine.  No SSH is used.  The runner
registers with no hostname (`--host ""`), so locally-claimed tasks attach
without SSH.  `make start` uses this form.

`--python` defaults to `sys.executable` (the interpreter running the CLI), so
the same virtual environment is reused automatically.

### Remote — Tunnel mode (default)

```
panopticon start-runner myhost
```

What happens:

1. SSH opens a reverse port forward: `localhost:<port>` on `myhost` reaches the
   local task service.
2. The remote `panopticon.sessionservice.host` is started with
   `--service-url http://localhost:<port>` (the forwarded address) and
   `--container-service-url http://host.docker.internal:<port>` (what Docker
   containers use to call back).
3. Docker's `--add-host host.docker.internal:host-gateway` (already injected by
   `LocalRunner`) routes `host.docker.internal` inside containers to the tunnel.

**Prerequisite**: `GatewayPorts clientspecified` (or `yes`) must be set in the
remote host's `/etc/ssh/sshd_config`, otherwise the tunnel only binds
`127.0.0.1` on the remote and containers cannot reach it.

```sshd_config
GatewayPorts clientspecified
```

After editing, reload: `sudo systemctl reload sshd`.

The constructed SSH command (for reference):

```sh
ssh \
  -R localhost:8000:localhost:8000 \
  -o ExitOnForwardFailure=yes \
  myhost \
  python -m panopticon.sessionservice.host \
    --service-url http://localhost:8000 \
    --container-service-url http://host.docker.internal:8000 \
    --runner-id myhost \
    --host myhost \
    --tasks-root ~/.panopticon/tasks \
    --cache-root ~/.panopticon/cache
```

### Remote — Direct mode (`--no-tunnel`)

For deployments where the task service is on a routable LAN address:

```
panopticon start-runner myhost \
  --no-tunnel \
  --service-url http://10.0.1.5:8000
```

No port forward is opened; the remote runner connects directly.
`--container-service-url` defaults to the same value as `--service-url` in
direct mode.

## Options

| Flag | Default | Description |
|---|---|---|
| `--local` | off | Run on this machine (no SSH) |
| `--service-url URL` | `$PANOPTICON_SERVICE_URL` or `http://localhost:8000` | Task service URL |
| `--remote-port PORT` | same as local port | Port forwarded on the remote host (remote only) |
| `--runner-id ID` | `<host>` or `local` | Runner id to register as |
| `--container-service-url URL` | derived | URL injected into containers |
| `--no-tunnel` | off | Skip the reverse port forward (remote only) |
| `--image IMAGE` | `panopticon-base` | Task container image |
| `--tasks-root PATH` | `~/.panopticon/tasks` | Tasks root directory |
| `--cache-root PATH` | `~/.panopticon/cache` | Cache root directory |
| `--python CMD` | `sys.executable` (local) / `python3` (remote) | Python interpreter; multi-word values are split (e.g. `uv run python`) |

## Keeping the runner alive

For a local runner, `make start` manages the lifecycle automatically via the
`runner` tmux session on the `panopticon` socket.

For a remote runner, the SSH session IS the tunnel.  Run it in a persistent
tmux pane alongside `make start`:

```
# in one tmux pane
make start

# in another pane
panopticon start-runner myhost
```

The runner reconnects automatically if `panopticon.sessionservice.host`
restarts, but if the SSH session dies the tunnel is lost.

## Future: `make start-remote`

A `REMOTE_HOST=myhost make start-remote` target could open a `start-runner`
pane alongside the existing `service` / `runner` / `dashboard` panes on the
`-L panopticon` tmux server, integrating remote runners into the standard
`make start` workflow.
