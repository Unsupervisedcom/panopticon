# `panopticon start-runner` — remote session service over SSH

`panopticon start-runner <host>` SSHes to a remote machine, opens a reverse
port forward so the remote runner (and its containers) can reach the local task
service, and starts `panopticon.sessionservice.host` there.  The SSH session IS
the tunnel — closing it stops the runner.

## Overview

The command solves one problem: the task service runs on the operator's machine
(or a known host), but compute may be on a remote machine.  A reverse port
forward means the remote machine never needs an inbound connection to the local
host — only a normal outbound SSH connection.

## Modes

### Tunnel mode (default)

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

### Direct mode (`--no-tunnel`)

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
| `--service-url URL` | `$PANOPTICON_SERVICE_URL` or `http://localhost:8000` | Local task service URL to expose to the remote runner |
| `--remote-port PORT` | same as local port | Port forwarded on the remote host |
| `--runner-id ID` | `<host>` | Runner id the remote session service registers as |
| `--container-service-url URL` | derived (tunnel: `host.docker.internal:<port>`, direct: `--service-url`) | URL injected into containers to reach the task service |
| `--no-tunnel` | off | Skip the reverse port forward |
| `--image IMAGE` | `panopticon-base` | Task container image on the remote host |
| `--tasks-root PATH` | `~/.panopticon/tasks` | Remote tasks root directory |
| `--cache-root PATH` | `~/.panopticon/cache` | Remote cache root directory |

## Keeping the runner alive

The SSH session is the tunnel.  Run `panopticon start-runner` in a persistent
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
