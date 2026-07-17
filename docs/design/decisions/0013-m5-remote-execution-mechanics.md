# 0013 — M5 remote execution mechanics: first slice (trusted network)

- Status: Accepted
- Date: 2026-07-08
- Deciders: Charlie Scherer

## Context

ADR 0008 settled the three-role topology and sketched Milestone 5 as "a runner per machine,
each a REST client of the central task service." ADR 0009 refined the terminal switching
model (detach→attach, not `switch-client`) and acknowledged several open questions: how
containers reach the task service from a remote host, how images get to a remote machine, how
runner hosts are discovered, how secrets are delivered, and how auth works across a network.

This ADR commits to the mechanics for the **first slice of M5**: getting a task to run on a
friendly remote machine — same operator, same trusted network, SSH access, able to build
images locally. This scoping keeps the first remote-execution increment small and verifiable.
Auth, registry, and secrets hardening are explicitly deferred to a later hardening slice.

### What's already in place

The codebase already has several M5-oriented pieces:

- `--container-service-url` (separate from `--service-url` in `host.py`) — the two-URL
  design that lets the runner tell containers to call back at a different address than the
  runner itself uses.
- `hold_runner_liveness` with reconnect backoff — runners maintain a persistent liveness
  stream to the task service and reconnect automatically.
- `startup_reclaim` — on runner restart, releases any claims on containers no longer running.
- `attach_command(session, *, socket, host=None)` in `terminal/attach.py` — already
  scaffolded to accept a remote host for ssh-wrapped attach.
- `POST /runners/{id}/reclaim` — operator-gated release of a dead host's task claims.
- Containers already use MCP/REST for all artifact access; no shared filesystem.

## Decision

### 1. Trusted-network scope for this slice

This slice assumes the remote runner is on a **trusted network** (same operator, SSH access,
no hostile traffic). Consequently:

- **No inter-process auth** between runners and the task service in this slice — the service
  trusts any caller, as it does today. Bearer tokens are deferred to a later hardening slice.
- **No image registry** — the remote machine builds composed images locally using the existing
  `ImageBuilder.build()` path. The base image (`make build`) must be present on the remote
  host before the runner starts (built there, `docker save | ssh | docker load`, or
  `DOCKER_HOST=ssh://` remote build). Registry push/pull is deferred.
- **No `--secrets-root` abstraction** — per-host secret delivery is handled by the operator
  copying files (see §4). A path-remapping flag is deferred.

With these three simplifications the only new mechanism for this slice is: the runner reports
its hostname at registration so the terminal controller knows where to SSH for remote attach
(§6 and §7).

### 2. Container → task-service callback

`--container-service-url` (already implemented in `host.py`) is the mechanism. A remote
runner sets this flag to the task service's routable address or hostname so containers can
call back for liveness/registration and MCP/REST operations. No reverse tunnel or NAT
traversal is required in this slice — the service must be reachable at a known URL from
inside Docker on the runner host. Operators who need NAT traversal can use any standard
tunnel; the code already supports it via this parameter.

### 3. Image distribution

The remote runner builds composed images locally using `ImageBuilder.build()`, the same path
the local runner uses. No registry. The base image must exist on the remote host before the
runner starts. Acceptable bootstraps:

- Build the base on the remote host directly (`make build` there).
- `docker save panopticon-base | ssh <host> docker load`
- `DOCKER_HOST=ssh://<host> docker build …` (ships the build context over SSH)

Registry push/pull (`docker push` / `docker pull`) is deferred to a later M5 slice.

### 4. Per-host secrets

`env_file` in the repo record is a host-local path. Operators must provision the file on each
runner host at the same absolute path stored in the DB record. Recommended layout:
`~/.config/panopticon/secrets/<repo-id>.env` on every host. The runner already uses whatever path
the DB record holds — no code change needed. A `--secrets-root` remapping flag (for operators
who can't keep identical paths across hosts) is deferred.

### 5. Artifact reach

Already resolved: containers use MCP/REST for all artifact access. The task service's
MCP/REST surface is the only artifact channel; no shared filesystem is needed. This was
established in ADR 0003/0006 and holds unchanged for remote execution.

### 6. Runner registration and host tracking

Runners register via `POST /runners/{id}/live` (already implemented). This slice adds a
`host` field to the runner registration payload. The task service stores it and surfaces it on
`GET /runners/{id}` (or on the task via `claimed_by` lookup). The terminal controller uses
this field to determine whether a task's session is local or remote (§7).

Discovery remains pull-initiated: each runner registers itself; the task service doesn't
discover or push to runners. Adding a machine means starting a runner there and pointing it at
the task service URL — no control-plane change.

### 7. Remote tmux attach

The terminal supervisor already uses `attach_command(session, socket=socket)` for local
sessions. For tasks on a remote runner, it calls:

```
ssh -t <host> tmux -L panopticon attach -t <session>
```

`attach_command()` in `terminal/attach.py` is already scaffolded for this (`host` parameter).
The supervisor passes `host=<runner.host>` when the task's runner is not the local machine.
The switching model (detach→attach loop from ADR 0009) is unchanged — only the attach command
gains an SSH prefix.

### 8. Failure and restart reconciliation

The existing story is correct and sufficient for this slice:

- **Runner restart**: `startup_reclaim` releases claims on containers no longer running;
  `hold_runner_liveness` reconnects the persistent liveness stream with backoff.
- **Task service restart**: in-memory registrations are lost. Containers and runners
  reconnect and re-register. A brief "disconnected" flash in the dashboard is acceptable —
  no persistence of registrations is needed.
- **Remote runner death**: the operator uses `POST /runners/{id}/reclaim` to release its
  claims. Any live runner picks up the newly unclaimed tasks.

### 9. Deferred to later M5 slices

- **Inter-process auth** — `PANOPTICON_API_KEY` bearer tokens between runners and the task
  service; per-task MCP token scoping for state-mutating operations (BACKLOG P1).
- **Image registry** — `ImageBuilder` push/pull; `--registry` arg on `host.py`; the
  `panopticon-<workflow>-<repo-id>` image name scheme is already in `images.py`.
- **`--secrets-root`** — path remapping for hosts that can't maintain identical env-file
  paths.
- **At-rest secret protection** — encryption of env-files at rest.

## Consequences

**Positive**

- First remote-execution increment is small: add `host` to runner registration, pass it
  through `attach_command`, and update `make start` / ops docs. Everything else already works.
- The trusted-network assumption is honest: most initial deployments are same-LAN or
  VPN-connected machines where auth overhead isn't the immediate concern.
- No control-plane change to add a host — the pull model scales as designed.
- The switching model (ADR 0009) is unchanged; remote attach is the same loop with an SSH
  prefix, exactly as designed.

**Negative / deferred**

- Without inter-process auth, any host on the network that knows the task service URL can
  register as a runner or mutate tasks. Acceptable for a trusted LAN; must be addressed before
  exposing the service across untrusted networks.
- Without a registry, operators must provision the base image on each host out-of-band. More
  ops friction, but no architectural compromise.
- Identical `env_file` paths across hosts is a mild constraint; `--secrets-root` can lift it
  in a later slice.

## Related

- ADR 0008 — the three-role topology; runner per machine; containers reach the task service
  via an injected URL. Resolves several of its open questions for the trusted-network case.
- ADR 0009 — terminal session supervisor (detach→attach switching); `attach_command` already
  scaffolded for `host=`; this ADR commits to using it.
- ADR 0005 — composed images; local build path used here; registry deferred.
- ADR 0007 — per-repo secrets; same-path convention; hardening deferred.
- ADR 0003/0006 — artifact reach and task service as sole DB authority; already correct for
  remote; no change.
- GOALS.md — Milestone 5 (remote execution).
