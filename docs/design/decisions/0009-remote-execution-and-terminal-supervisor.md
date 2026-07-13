# 0009 — Remote execution: per-host runners + a terminal session supervisor

- Status: Accepted
- Date: 2026-06-15
- Deciders: Charlie Scherer

## Context

ADR 0008 settled the topology — task service, runner (session service), and terminal
controller as three separate host processes — and sketched Milestone 5 remote execution as "a
runner per machine, each a REST client of the central task service; the terminal controller
reaches a remote task's tmux over ssh." It deliberately left the mechanics as open questions:
how a container reaches the task service, how runners are discovered, how images get to a remote
host, and — crucially — how the terminal *switches* to a session that lives on another machine.

This ADR commits to those mechanics. One of them, the terminal's switching model, must be
decided **now**, even though remote execution is M5, because it changes local keybinding/UX
behaviour. Today the dashboard hands off with tmux `switch-client`, which only moves a client
*within a single tmux server* and therefore can never reach another host. Building the local
terminal around `switch-client` means building something we must tear out for M5.

## Decision

### 1. Remote spawn: a runner per host that *pulls* (not a central push)

Reaffirming ADR 0008's "runner per machine": each host runs a `sessionservice` runner that is a
REST client of the single central task service, **pulls** its assigned work, and spawns
containers on its **own local Docker daemon** with its **own local tmux**. The task service
never SSHes out and never touches a remote daemon — it stays the side-effect-free control plane
and sole DB authority (ADR 0006). Adding a machine = starting a runner there and registering it;
no control-plane change.

Rejected alternative — the **task service (or a central runner) SSHes out** to build images and
start containers on remote hosts: simpler to picture, but it re-couples the control plane to
remote side effects, needs SSH fan-out + credentials to every host, and puts the agent's tmux on
the wrong side of the network (see §3). The pull model keeps each host self-contained and is
NAT-friendly — runners dial out; nothing dials in except the operator's ssh for *viewing*.

### 2. Container → task-service callback: reverse tunnel or routable URL

A remote container must reach the central task service for liveness/registration and MCP/REST
(ADR 0003/0006). The protocol is already transport-agnostic; only *reachability* is new. The
runner — which already reaches the service to pull work — injects a service URL the container can
use. Preferred: a **reverse SSH tunnel** (or the runner proxying), so the service needs no public
exposure and the host needs only outbound connectivity. Where the network already permits it, a
**routable service address + authentication** (inter-process auth is an ADR-0008 open question
M5 must answer regardless).

### 3. Agent persistence lives where the container runs

tmux runs at the runner level **on the host where the container runs** (ADR 0008), so the agent
— a `docker exec` process inside that tmux pane — survives network drops: ssh is only the
operator's viewing attachment, and losing it merely detaches the view while the agent keeps
running. Running the pane's tmux locally and reaching the container over `ssh … docker exec`
would tie the agent's life to the ssh connection — rejected.

### 4. Image distribution

Composed images (ADR 0005) must exist on the runner's host. Preferred: a **registry** — build
once, `docker pull` per host. Acceptable bootstraps: build on the host via `DOCKER_HOST=ssh://`
(ships the build context), or `docker save | ssh | docker load`.

### 5. Per-host secrets

ADR 0007 references (`env_file` path, `creds_volume`) are **per host** — they must exist on the
host whose runner launches the container, and `panopticon login` targets that host's daemon. A
repo's secret references therefore gain a host dimension at M5.

### 6. The terminal controller is a *session supervisor* (adopted at M1)

The terminal controller becomes a long-lived **supervisor that owns the TTY** and routes the
operator between sessions by **detach→attach**, never `switch-client`. **The dashboard itself
runs in a tmux session** (`dashboard`, on the panopticon socket) alongside the task sessions, so
the whole console is one tmux server the operator can reach by attaching it.

- It runs a **hub-and-spoke** loop: attach the dashboard session; when the operator picks a task
  (`t`), the dashboard records the chosen session and **detaches its client — staying alive in
  the background** — returning control to the supervisor, which **attaches** the terminal to the
  task (local: `tmux -L panopticon attach -t <session>`; remote at M5: `ssh -t <host> tmux -L
  panopticon attach -t <session>`); when the operator detaches the task (tmux's detach key), the
  supervisor re-attaches the **same, still-running** dashboard — cursor and scroll preserved.
- Switching is therefore *always* detach→attach. With no `switch-client`, a session on another
  host is reached by the same loop with an ssh-wrapped attach — the only remote difference is a
  command prefix.
- Because the dashboard runs *inside* tmux it can't hand its choice back in-process, so it writes
  the chosen session to a small **switch-file** the supervisor reads once the dashboard detaches.
  A deliberate, tiny control channel — the price of keeping the dashboard a real, persistent tmux
  session (rather than relaunching it each switch, which would lose its state).

This is adopted now, local-only, because it fixes the switching/keybinding model: the dashboard's
`t` no longer switches a client in place; the dashboard detaches (staying alive) and the
supervisor hands the terminal to the task, and the operator returns by detaching it.

Rejected alternatives for switching: **`switch-client`** (cannot cross tmux servers/hosts — the
exact thing to avoid); **one outer tmux with `ssh … tmux attach` panes** (nested-tmux prefix
collisions). **Considered but not chosen:** running the dashboard as a direct (non-tmux) child
that returns its pick in-process — it removes the switch-file, but the dashboard is then *not* a
tmux session, breaking the "one panopticon tmux server holding the dashboard and every task,
reachable by attaching it" model we want; the switch-file is the accepted cost of keeping it.

## Why this shape

- **Remote is deployment, not a rewrite** (continues ADR 0008): same components, pull runners,
  ssh only for the human's view.
- **The control plane stays pure** — no outbound SSH, no remote daemon access; still the sole DB
  authority.
- **The agent is resilient to flaky networks** because its tmux is co-located with its container.
- **One switching mechanism for local and remote.** Deciding it now avoids building — and then
  unlearning — a `switch-client`-shaped terminal.

## Consequences

**Positive**
- Local and remote attach are the same loop; M5 adds an ssh prefix, not a new model.
- The supervisor is small, stateless, and independently testable — inject the dashboard step
  and the attach; the loop is just `while (s := show_dashboard()) is not None: attach(s)`.
- No control-plane change to add a host.

**Negative / open questions**
- Local-only cost: same-host switches become detach→reattach (a screen redraw) instead of an
  instant `switch-client`. Acceptable; could return as a same-host optimisation if it grates.
- Reverse-tunnel vs. exposed-service-plus-auth, runner registration/discovery + capacity, and
  artifact reach for remote runners remain ADR-0008 open questions to settle when M5 is built.
- Image distribution wants a registry; standing one up is M5 work.
- Per-host secrets complicate `login` and repo configuration at M5.

## Related

- ADR 0008 — the topology this refines (runner per machine; terminal reaches a remote tmux over
  ssh); resolves several of its open questions.
- ADR 0006 — task service as sole DB authority; runners are pull clients.
- ADR 0005 — composed images that must reach the runner's host.
- ADR 0007 — per-task secrets, now per host at M5.
- ADR 0003 — artifacts via the task service's surface (relevant to remote reach).
- ADR 0002 — the dashboard/terminal controller as a substitutable REST client, now a supervisor.
- GOALS.md — Milestone 5 (remote execution).
