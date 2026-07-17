# `github-self-reviewed`

Ships a change as a GitHub pull request that **you review yourself**. It's the same as
[`github-peer-reviewed`](github-peer-reviewed.md) but without the separate review gate.
There's no peer to wait on: you review the change during or after implementation and
approve it by advancing the task to merging.

```
PLANNING → ITERATING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** GitHub-hosted changes you review yourself, with no peer-review gate.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **PLANNING** | The agent collects requirements and writes a `plan.md` artifact (read it from the dashboard: highlight the task and press `a`) plus a token estimate. | **You**, by approving the plan with `/advance`. |
| **ITERATING** | The agent implements the plan, commits and pushes, opens a **draft PR**, and gets CI green. You self-review the change here. | **You**: advancing to MERGING *is* your approval. |
| **MERGING** | The agent adds the PR to the merge queue and re-adds it if it falls out. | **The agent**, which advances itself once the PR is merged. |
| **COMPLETE** | Terminal. The change has landed. | n/a |

There's no REVIEW state: with self-review, "tell the agent to proceed to merging" is the
approval. You can also send the task back to ITERATING from MERGING at any time if you spot
something.

## Your part and the agent's part

- **You**: approve the plan, review the change yourself, and advance out of ITERATING when
  it's good to merge. Nothing merges until you do.
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

- [`github-peer-reviewed`](github-peer-reviewed.md): the same flow with a peer-review gate.
- [`local-git-self-reviewed`](local-git-self-reviewed.md): the same self-review model, but
  local git only (no PR).
- [Workflow catalog](README.md).
