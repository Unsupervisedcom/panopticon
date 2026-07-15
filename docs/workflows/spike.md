# `spike`

**Open-ended agent work with no process gates.** A single working state that runs until
you decide it's done. There's no planning step, no responsibilities to satisfy, and no
forge skills, just the agent working with you until you're satisfied.

```
ITERATING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** explorations, debugging sessions, and research. Anything where a
plan-review-merge lifecycle would only get in the way.

This workflow is **shown for every repo by default** (no opt-in needed).

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **ITERATING** | The agent works on whatever you ask, back and forth, with no gates. | **You**, by marking it COMPLETE (`/advance`) when you're satisfied. |
| **COMPLETE** | Terminal. | n/a |

The agent waits for your first instruction on entry, then works until you call it done.
Nothing merges or ships on its own; a spike is a workspace, not a delivery pipeline.

## Your part and the agent's part

- **You**: drive the session and decide when it's finished.
- **The agent**: does the work you ask, with no responsibilities to gate it.

## Skills

None beyond the universal ones every task has. A spike is deliberately bare.

## Related

- [`orchestrator`](orchestrator.md): the other ungated, single-state workflow, but one
  that spawns and pre-plans child tasks.
- [Workflow catalog](README.md).
