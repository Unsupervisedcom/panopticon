# panopticon

Orchestrate multiple coding agents across isolated tasks and **configurable workflows**.

A ground-up rewrite of the [cloude-cade](https://github.com/tildesrc/cloude-cade)
prototype. The design lives on the [`design-docs`](../../tree/design-docs) branch
(goals, parity analysis, architecture, roadmap, and ADRs).

## Architecture in one paragraph

A deterministic control plane (the **task service**) owns task state and drives
per-workflow state machines; a per-machine **runner** spawns task containers and host
tmux sessions; a **terminal controller** runs the dashboard. **All LLM calls happen
inside task containers** — the control plane, runner, and dashboard never call a model.
See the `design-docs` branch for the full picture.

## Status

Early development. Building Milestone 1 in vertical slices (see the roadmap). This slice
lands the core contracts:

- `panopticon.core` — domain models, the workflow port (`Workflow` ABC), and the
  deterministic lifecycle engine (state machine, turn tracking, responsibility gating).
- `panopticon.workflows.FreeFormWorkflow` — the minimal seed workflow.

## Development

```sh
uv sync              # create the venv and install dev deps
uv run pytest        # run tests
uv run mypy -p panopticon   # type-check
```
