# Workflows: choosing how a task runs

Every task runs a **workflow**: the lifecycle that decides what states the task moves
through, **who advances each one** (you or the agent), what the agent must finish before
it can move on (its *responsibilities*), and which extra *skills* the agent has in its
container. Picking a workflow is how you set the line between what the agent may do on
its own and what needs your sign-off.

You choose a workflow when you create a task. On the dashboard press `n`, pick the repo,
then pick the workflow. This page is the catalog; each workflow has its own page with
the details. For the *task* itself — the object a workflow drives, its properties, and its
lifecycle — see [Tasks](../tasks.md).

## The built-in workflows

| Workflow | What it does, and when to use it | Where it ships |
|---|---|---|
| [`github-peer-reviewed`](github-peer-reviewed.md) | Ships a GitHub PR that a **peer** reviews before it merges. Use for GitHub changes that need a second person's sign-off. | A GitHub PR (peer-approved) |
| [`github-self-reviewed`](github-self-reviewed.md) | Ships a GitHub PR that **you** review yourself, with no peer-review gate. | A GitHub PR (self-approved) |
| [`local-git-self-reviewed`](local-git-self-reviewed.md) | Keeps the work **local**: commits to a branch and merges it, with no GitHub, PR, or CI. Use when the change never leaves the machine. | A local branch merged into the base |
| [`spike`](spike.md) | **Open-ended** agent work with no gates. Use for exploration, debugging, and research, until you call it done. | Nothing lands on its own |
| [`orchestrator`](orchestrator.md) | An agent that **decomposes a goal into child tasks**, each pre-planned and handed to you ready to approve. Use to fan work out across agents. | New pre-planned child tasks |
| [`setup-repo`](setup-repo.md) | A host-side **setup utility** (no container) that mints a repo's `claude` auth token. Launched from the repos screen, not the task picker. | A token in the repo's env-file |

Any task can also be **dropped** at any time (dashboard `x`), which moves it to `DROPPED`
without merging or shipping anything.

## The planning step

Four change-making workflows (`github-peer-reviewed`, `github-self-reviewed`,
`local-git-self-reviewed`, and, per child, `orchestrator`) start in **PLANNING**. Before
the agent can leave that state it must:

- **Write a plan** as the task's `plan.md` artifact. This is your chance to redirect
  before any code is written.
- **Record a token estimate** so the task's projected cost is tracked.

The plan is a task **artifact**, which you read from the dashboard: highlight the task and
press `a` to open its `plan.md`. (Artifacts are the task's own documents, kept with the
task rather than in the repo, so the dashboard is where you read them.)

You approve the plan by advancing the task out of PLANNING (attach with `t`, run
`/advance`). `spike` and `setup-repo` have no planning step.

## Who advances a state

Each state is advanced by either **you** or the **agent**:

- **You advance** the foreground states (plan approval, sign-off before merge). The agent
  fills in its responsibilities and then waits; nothing proceeds until you say so. Attach
  with `t` and run `/advance` to approve, and the agent starts a fresh turn.
- **The agent advances** the background states (merging). Once its responsibilities are
  met (the PR is merged, or the branch is merged locally) it moves the task on by itself.

## How workflows are offered

- **Default-on vs. opt-in.** `spike` and `orchestrator` are shown for every repo by
  default. `github-peer-reviewed`, `github-self-reviewed`, and `local-git-self-reviewed`
  are **opt-in**: enable them per repo (in the repo's workflow settings) before they show
  up in the task-creation picker.
- **Hidden utilities.** `setup-repo` is hidden from the pickers entirely; you launch it
  from the repos screen's setup hotkey, not by creating an ordinary task.

## Adding your own

Workflows are just `Workflow` subclasses. Drop a module defining one into
`~/.config/panopticon/workflows/` (or point the session service at a directory with
`--workflows-path`) and it registers automatically, with no change to the core service. See
[ADR 0004](../design/decisions/0004-workflow-abstraction.md) and
[`docs/design/ARCHITECTURE.md` §7](../design/ARCHITECTURE.md) for the authoring model.
