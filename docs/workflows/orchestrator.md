# `orchestrator`

An agent that **decomposes a high-level goal into several child tasks** and seeds each one
pre-planned, ready for you to approve. Use it to fan work out across multiple agents
without hand-writing each task.

```
ORCHESTRATING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** you have a big request that splits into parallel pieces of work and you'd
rather review a batch of ready-to-go plans than create each task by hand.

This workflow is **shown for every repo by default** (no opt-in needed).

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **ORCHESTRATING** | The agent breaks the request into child tasks and, for each, creates it, writes its `plan.md`, names it, records a token estimate, and hands its turn to you. | **You**, by marking it COMPLETE (`/advance`) once it has spawned everything. |
| **COMPLETE** | Terminal. | n/a |

Each child task lands in **PLANNING** with its plan already written and the planning gate
cleared, so all you do is read the plan and advance it. The usual targets are
[`github-self-reviewed`](github-self-reviewed.md) / [`github-peer-reviewed`](github-peer-reviewed.md)
tasks that arrive pre-planned.

## Your part and the agent's part

- **You**: give the high-level goal, then review and approve each spawned child task.
- **The agent**: decomposes the goal, creates and pre-plans the children, and hands each
  to you. Unlike an ordinary workflow's agent, it's allowed to **create tasks** (the
  `orchestrates` capability).

## Skills

- **`spawn-task`** creates a new child task and seeds it with a plan, ready for you to
  approve.
- **`review-task`** reviews a spawned task's change and either approves it or leaves a
  `review.md` artifact on that task with findings.

## Related

- [`spike`](spike.md): the other ungated, single-state workflow (does the work itself
  rather than spawning tasks).
- [Workflow catalogue](README.md).
