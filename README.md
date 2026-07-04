# panopticon

Orchestrate multiple coding agents across isolated tasks and **configurable workflows**.

A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype.

## Architecture in one paragraph

A deterministic control plane (the **task service**) owns task state and drives
per-workflow state machines; a per-machine **runner** spawns task containers and host
tmux sessions; a **terminal controller** runs the dashboard. **All LLM calls happen
inside task containers** — the control plane, runner, and dashboard never call a model.
See the `design-docs` branch for the full picture.

## Status

Early development. Building Milestone 1 in vertical slices (see the roadmap). **Slice 1**
lands the four contracts plus a walking skeleton:

- `panopticon.core` — domain models, state classes, the `Workflow` interface (the
  deterministic state machine: resolution, turn tracking, responsibility gating), and the
  store & artifact interfaces.
- `panopticon.taskservice` — the control plane: `TaskService`, a FastAPI REST API, the
  SQLAlchemy store adapter (in-memory or on-disk SQLite), the filesystem artifact store, and
  the MCP surface contract.
- `panopticon.sessionservice` / `panopticon.container` — a stub runner and the container
  entrypoint protocol that drive the end-to-end walking skeleton (no Docker, no LLM yet).
- `panopticon.workflows.Spike` — the minimal seed workflow.

## Development

```sh
uv sync              # create the venv and install dev deps
uv run pytest        # run tests
uv run mypy -p panopticon   # type-check
```
