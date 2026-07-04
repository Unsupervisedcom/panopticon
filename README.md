# panopticon

Orchestrate multiple coding agents across isolated tasks and **configurable workflows**.

A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.

## Architecture in one paragraph

A deterministic control plane (the **task service**) owns task state and drives
per-workflow state machines; a per-machine **runner** spawns task containers and host
tmux sessions; a **terminal controller** runs the dashboard. **All LLM calls happen
inside task containers** — the control plane, runner, and dashboard never call a model.
