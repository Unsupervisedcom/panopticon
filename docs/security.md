# Security posture

Panopticon is **self-hosted and single-operator by design**: your machine, your secrets, your
repos, one trusted person at the console. This page states the trust model plainly, including where
it currently stops. Read it before you expose any part of the system beyond your own machine.

The short version: agents are sandboxed and quarantined on branches, but the control plane trusts
whoever can reach it. That's fine for one operator on a private host and **not** yet safe to put on
a shared network. The rest of this page is the detail.

## Isolation: what contains an agent

Each task runs in its own Docker container, on its own clone of the repo, on its own branch
(`panopticon/<slug>`). Agents never share a working tree, so one task cannot see or corrupt
another's files.

Inside that container the agent runs `claude --dangerously-skip-permissions`
([`container/agent.py`](../src/panopticon/container/agent.py)): it never stops to ask permission.
That is deliberate — there is no operator watching each container to answer prompts, and the blast
radius is the task's own throwaway clone. The sandbox is the container, not claude's permission
prompt.

Nothing an agent writes reaches your `main` branch on its own. Work stays quarantined on the task's
branch until **you** review it and advance the task through its workflow's review and merge steps.
That operator gate is the point of the workflows: you own what ships. See the
[workflow catalog](workflows/README.md).

## Elevated capabilities: `docker_in_docker`

A repo can opt into the `docker_in_docker` capability. When set, the runner spawns that repo's
containers with `--privileged` and the entrypoint starts a nested Docker daemon
([`sessionservice/local_runner.py`](../src/panopticon/sessionservice/local_runner.py),
[`docker/entrypoint.sh`](../src/panopticon/docker/entrypoint.sh)).

`--privileged` is effectively host root: a privileged container can escape to the host. This is a
real trust escalation, so it is **off by default** and you should enable it only for repos whose
code and agents you already trust to run on the host. See [`repos.md`](repos.md) and
[`layers.md`](layers.md).

## Networking

Containers reach the host control plane at `host.docker.internal`, wired in by the runner with
`--add-host` ([`sessionservice/local_runner.py`](../src/panopticon/sessionservice/local_runner.py)).

The task service listens on a host port. **Its default bind is `0.0.0.0:8000` — all interfaces**
([`taskservice/__main__.py`](../src/panopticon/taskservice/__main__.py)), which on a machine with a
public or shared network interface means anything on that network can reach it. There is no
authentication in front of it (see the next section), so **treat the bind address as a security
control**: keep it on a private host, bind it to loopback (`PANOPTICON_HOST=127.0.0.1`), or firewall
the port. Do not expose it to an untrusted network.

## Trust boundary: the control plane trusts its callers

This is the current limitation, stated plainly. **The task service does not authenticate callers.**
A request names a `task_id` and mutates that task, over REST or MCP; nothing checks that the caller
is entitled to it. Because in-container agents reach the shared MCP server, one task's container can
name another task's id and mutate it. The MCP server also disables the SDK's DNS-rebinding guard so
containers can reach it across the `host.docker.internal` boundary
([`taskservice/mcp.py`](../src/panopticon/taskservice/mcp.py)).

For a single trusted operator on a private host this is acceptable: every task is yours, and the
containers you run are ones you launched. It becomes a real concern the moment you add multiple
users, run untrusted repos, or split runners across machines on a shared network. This gap is
tracked as a P1 cleanup — a per-task secret issued at spawn and required on state-mutating
tools/endpoints — in [`docs/design/BACKLOG.md`](design/BACKLOG.md). Until that lands, do not rely on
task isolation as a security boundary against a hostile task.

## Secrets

Each repo's credentials live in a host env-file, referenced by name, never stored in the database or
in task artifacts. The setup flow writes it `0600` (owner-only) under the secrets dir, and the
runner injects it into the container with `docker run --env-file`
([`sessionservice/local_runner.py`](../src/panopticon/sessionservice/local_runner.py)). A task gets
only its own repo's env-file, resolved on the runner's host, so the value stays host-local.

The env-file carries the container's `CLAUDE_CODE_OAUTH_TOKEN` (a long-lived, non-rotating
`claude setup-token`) alongside any `ANTHROPIC_API_KEY` / `GH_TOKEN`. Because the token is
long-lived, **rotation is manual**: re-mint with `claude setup-token` and overwrite the env-file.
There is no automatic expiry or revocation from panopticon's side. Full detail in [`auth.md`](auth.md).

## What leaves your machine

The control plane makes **no LLM or external model calls**. Every model call happens inside a task's
container, which talks directly to the model provider; the packages that run on your host (`core`,
`taskservice`, `sessionservice`, `terminal`, `workflows`) are LLM-free, and the only LLM-bearing
package is `container/`. This is the determinism invariant, and it is also a data-flow boundary: your
code and prompts leave the machine only from inside a task container, to the provider that repo is
configured for. See [`overview.md`](overview.md).

## Checklist before you widen the trust radius

- Keep the task service off untrusted networks: bind it to loopback or firewall the port. The
  default `0.0.0.0` bind plus no auth means an exposed port is an open control plane.
- Enable `docker_in_docker` only for repos you trust to run as host root.
- Treat the OAuth token as a long-lived secret; rotate by re-minting when it may have leaked.
- Don't run untrusted repos or share the install between users yet — task-to-task isolation is not
  an enforced security boundary (see the trust-boundary gap above).
