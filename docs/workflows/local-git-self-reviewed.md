# `local-git-self-reviewed`

Keeps the work **entirely local**: no GitHub, no pull request, no CI, no remote merge
queue. The agent commits to the task branch, you review the diff yourself, and the agent
merges the branch into the base branch. Use it for repos where the change never needs to
leave the machine.

```
PLANNING → ITERATING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** local commits only, no remote push or PR. The work stays in the local
repo; you approve the diff and the agent merges the branch.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **PLANNING** | The agent collects requirements and writes a `plan.md` artifact (read it from the dashboard: highlight the task and press `a`) plus a token estimate. | **You**, by approving the plan with `/advance`. |
| **ITERATING** | The agent implements the plan and commits to the task branch. You self-review the diff (`git diff` / `git log` locally). | **You**: advancing to MERGING *is* your approval. |
| **MERGING** | The agent merges the task branch into the repo's base branch. | **The agent**, which advances itself once the merge lands. |
| **COMPLETE** | Terminal. The change is merged locally. | n/a |

If the merge hits conflicts the agent can't resolve, it sends the task back to ITERATING
with an explanation.

## Your part and the agent's part

- **You**: approve the plan, review the local diff, and advance out of ITERATING when it's
  good to merge.
- **The agent**: plans, implements, commits, and, once you approve, merges the branch
  and advances to complete.

## Skills

- **`local-merge`** merges the task branch into the repo's base branch (typically `main`)
  with a merge commit. On conflicts it returns the task to ITERATING; on success it
  advances to complete.

There's no `gh` tool and no PR/CI plumbing. That's the point of this workflow.

## Related

- [`github-self-reviewed`](github-self-reviewed.md): the same self-review model, but ships
  a GitHub PR.
- [Workflow catalog](README.md).
