# panopticon

**Mission control for a fleet of coding agents.**

Panopticon helps you delegate work to many agents while staying in the loop on each of
them. Agents move back and forth between autonomous work and moments where they need your
input; without a single place to watch them, a fleet drifts idle and the work in progress
becomes impossible to track. Panopticon gives you that place.

- **A live dashboard** of all your tasks — who's working, who's waiting on you.
- **Configurable workflows** that set the boundary between what an agent may do alone and
  what requires your sign-off.
- **Frictionless task creation** — isolated containers, managed branches, and per-repo
  secrets and environment, out of the box.
- **Reflection** — agents that help you plan tasks and recap completed ones.

Self-hosted and model/CLI-agnostic — your infrastructure, your secrets, your repos. A
ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade) prototype.

## Architecture in one paragraph

A deterministic control plane (the **task service**) owns task state and drives
per-workflow state machines; a per-machine **runner** spawns task containers and host
tmux sessions; a **terminal controller** runs the dashboard. **All LLM calls happen
inside task containers** — the control plane, runner, and dashboard never call a model.
