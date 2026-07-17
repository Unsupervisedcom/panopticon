# `github-peer-reviewed`

Ships a change as a GitHub pull request that a **peer reviews before it merges**. This is
the full lifecycle (plan, implement, review, merge), and the one to reach for when a
second person signs off on the code.

```
PLANNING → ITERATING → REVIEW → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** GitHub-hosted changes that require a peer review before merging.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **PLANNING** | The agent collects requirements and writes a `plan.md` artifact (read it from the dashboard: highlight the task and press `a`) plus a token estimate. | **You**, by approving the plan with `/advance`. |
| **ITERATING** | The agent implements the plan, commits and pushes, opens a **draft PR**, and gets CI green. | **You**, once you're happy the change is ready for review. |
| **REVIEW** | The task waits for the PR to be reviewed and approved by a peer. | **You**, once the PR is reviewed. |
| **MERGING** | The agent adds the PR to the merge queue and re-adds it if it falls out. | **The agent**, which advances itself once the PR is merged. |
| **COMPLETE** | Terminal. The change has landed. | n/a |

Going back to coding from REVIEW or MERGING isn't a wrong turn: you (via the agent) can
move the task straight back to ITERATING at any time. It doesn't have to follow the arrows.

## Your part and the agent's part

- **You**: approve the plan, decide when the change is ready for review, and get a peer to
  review the PR, then advance through REVIEW. Nothing merges until you've moved it along.
- **The agent**: does the planning, coding, testing, PR authoring, CI-watching, and merge
  shepherding. In MERGING it drives to a merge on its own.

## Skills

The agent has these forge skills in its container (it runs `gh` against GitHub):

- **`open-pr`** pushes the branch and opens a draft PR, then records the PR URL on the
  task (so the dashboard's `p` hotkey opens it).
- **`babysit-ci`** watches the PR's CI and fixes failures (and base conflicts) until it's
  green, without tying up a turn while CI runs.
- **`babysit-merge`** shepherds the PR through the merge queue, re-queuing if it's kicked
  out, until it merges.

## Related

- [`github-self-reviewed`](github-self-reviewed.md): the same flow without the peer-review
  gate; you review it yourself.
- [`local-git-self-reviewed`](local-git-self-reviewed.md): no GitHub at all.
- [Workflow catalog](README.md).
