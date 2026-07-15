# How panopticon works

Panopticon lets you run a fleet of coding agents and watch them all from one place. Agents
write the code; **you own what ships**. This page is the mental model behind that promise:
what's actually running on your machine, and how a task flows through it. If you just want
to get going, start with the [README](../README.md) and the
[workflow catalog](workflows/README.md); come back here when you want to understand the
shape of the system.

## The core idea: mission control, not an agent

The part of panopticon that *you* run (the dashboard and the plumbing behind it) never
calls a language model. Every LLM call happens inside a task's own container. Panopticon is
mission control: it tracks what each agent is doing, whose turn it is, and what has to be
true before anything moves forward. The agents live one layer down, each boxed into its own
container.

Keeping the model out of the control layer is what makes the rest of the experience hold up:

- **The dashboard stays fast and trustworthy.** It's plain, deterministic software reading
  task state, with no model in the loop to stall or surprise you.
- **Cost and credentials are scoped.** Model usage and secrets are confined to containers,
  where each repo's credentials are injected just for that task.
- **Nothing an agent does can corrupt the source of truth.** Agents ask the control plane to
  record results and move tasks along; they never write task state directly.

## The pieces on your machine

Four things run when you bring panopticon up:

| Piece | What it is | What you see |
|---|---|---|
| **Control plane** | The brain and single source of truth: every task's state, whose turn it is, its history, and its plan. Nothing changes a task except by going through it. | The data behind the dashboard |
| **Runner** | The per-machine worker. For each task it spawns a container, injects that repo's secrets, and creates the task's branch. One runner per machine, which is what lets a fleet span machines. | Tasks moving `queued → … → live` |
| **Task containers** | Where each agent actually runs: one per task, sandboxed, each on its own branch, so an agent can work freely and nothing reaches your main branch without your review. | A session you can attach to |
| **Dashboard** | Your window on the fleet, in the terminal. Create tasks, watch turns, and drop into any task's session. | The screen you drive |

Put together, it looks like this:

```
     you
      │  (terminal)
      ▼
 ┌──────────┐        ┌─────────────────┐        ┌──────────┐
 │dashboard │ ◀────▶ │  control plane  │ ◀────▶ │  runner  │
 └──────────┘        │ (source of      │        └────┬─────┘
      │              │  truth)         │             │ spawns
      │ attach (t)   └─────────────────┘             ▼
      │                                     ┌───────────────────┐
      └────────────────────────────────────▶│  task containers  │
                                            │  agent + branch   │
                                            │  (one per task)   │
                                            └───────────────────┘
```

You talk to the control plane through the dashboard. The runner talks to it to pick up work
and report progress. The agents, inside their containers, ask it to record what they've done
and to move their task forward. The control plane is the only thing that writes task state;
everything else goes through it.

## The concepts you'll meet

These are the terms you'll see on the dashboard, in brief. Each has a dedicated guide that
goes deeper; this section is just the map.

- **Task, and its slug.** A task is one unit of work. You describe it in a sentence or two;
  the agent gives it a short **slug** (a human-friendly name), and that slug names its branch
  (`panopticon/<slug>`). You'll see the slug on the dashboard. [`docs/tasks.md`](tasks.md) is
  the full reference for the task object, its states, turns, and responsibilities.
- **Branch and sandbox.** Every task gets its own clone of the repo and its own branch. That's
  the isolation: agents never share a working tree, and their work is quarantined on a branch
  until you're happy with it.
- **Workflow.** The lifecycle a task follows: what states it passes through, **who advances
  each one** (you or the agent), and what the agent must finish before it can move on. Picking a
  workflow is how you set the line between what an agent may do alone and what needs your
  sign-off. See the [workflow catalog](workflows/README.md).
- **Turn.** At any moment a task's turn is either the **agent's** (it's working) or **yours**
  (it's waiting on you). The `turn` column tells you, at a glance, which tasks need you, so you
  stop hunting through terminals for the one that's stuck. A task can also be **blocked**: a
  deliberate "waiting on something" marker the agent raises.
- **Responsibilities.** Each workflow state gives the agent a checklist it must finish before
  the task can advance: write a plan, get tests passing, get CI green. A task won't move on
  until they're all met, which is why it sometimes sits and waits.
- **Artifacts.** A task's own documents, most importantly its **plan** (`plan.md`), kept with
  the task rather than in the repo. You read them from the dashboard: highlight the task and
  press `a`.

## The life of a task

Here's the whole arc, and where each step shows up on the dashboard:

1. **You create it.** Press `n`, pick the repo and a workflow, and describe the work. The task
   appears with no branch yet.
2. **The runner starts it.** The `container` column moves `queued → … → live` as the runner
   builds the container, injects the repo's secrets, and starts the agent. See
   [`docs/container.md`](container.md) for what each container status means and how a
   container recovers if it dies.
3. **The agent plans.** It names the task (setting the slug and creating its branch), then writes
   a plan. When the `turn` column shows the task is waiting on you, press `a` to read the plan.
   This is your chance to redirect before any code is written.
4. **You approve the plan.** Attach to the task with `t`, run `/advance` to accept the plan (or
   steer the agent first), and detach with `Ctrl-b d`. The agent starts a fresh turn and begins
   implementing.
5. **The agent works.** It writes code on its branch, runs tests, opens a PR, and shepherds CI,
   all inside its container and reporting progress back to the control plane. It handles the steps a
   workflow lets it do on its own; it stops and waits for you at the steps that need your
   sign-off.
6. **You review what ships.** For a GitHub workflow, press `p` to open the PR in your browser; for
   a local workflow, diff the task's branch. Nothing lands until you advance it: you own what
   ships.
7. **It merges.** Once you've signed off, the task moves to its merge step and finishes.

Throughout, you can drop any task with `x` (it moves to `DROPPED` without shipping anything),
and re-attach to any running task with `t` to see what it's doing.

## Where your data lives

Panopticon is **self-hosted**: your infrastructure, your secrets, your repos. It keeps its
database, task artifacts, and per-task clones under standard locations on your machine (all
overridable; see the [Configuration table in the README](../README.md#configuration)). Each
repo's secrets are stored per repo as a reference the runner resolves on its own host and
injects only into that repo's tasks; the values never enter the database or the artifacts.
[`docs/repos.md`](repos.md) covers how a repo is configured, secrets and all.

## Running a fleet across machines

The control plane stays single: one source of truth. To spread work across more machines, you
run a **runner** on each one, pointed at that same control plane. Each runner spawns and owns the
containers on its host, and the dashboard reaches a remote task's session over SSH. Adding
capacity is just starting another runner; there's no change to the control plane.

## Where to go next

This page is the map; these guides are the detail:

- **[Workflow catalog](workflows/README.md)** — the built-in workflows and how to choose (and
  how to add your own).
- **[Tasks](tasks.md)** — the task object in full: its properties, states, and lifecycle.
- **[Containers](container.md)** — the container lifecycle, every dashboard status, and recovery.
- **[Repos](repos.md)** — configuring a repo: secrets, image layers, and capabilities.
- **[README](../README.md)** — install, quickstart, your first task, and configuration.
