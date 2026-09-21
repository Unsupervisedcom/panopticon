# `local-git-self-reviewed`

No pull request, no CI, no remote merge queue. The agent commits to the task branch, you
review the diff yourself, and the agent merges the branch into your base branch and pushes
the result back into the repo the task was cloned from. Use it for repos where the change
never needs to become a PR.

```
PLANNING → ITERATING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** no PR and no CI. You approve the diff, and the agent merges the task branch
into your repo's base branch and pushes it back to the repo the task was cloned from.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **PLANNING** | The agent collects requirements and writes a `plan.md` artifact (read it from the dashboard: highlight the task and press `a`). | **You**, by approving the plan with `/advance`. |
| **ITERATING** | The agent implements the plan and commits to the task branch. You self-review the diff (`git diff` / `git log` locally). | **You**: advancing to MERGING *is* your approval. |
| **MERGING** | The agent merges the task branch into the repo's base branch, then has it pushed back to your repo. | **The agent**, which advances itself once the push lands. |
| **COMPLETE** | Terminal. The change is in your repo. | n/a |

If the merge hits conflicts the agent can't resolve, or the push is refused, it sends the
task back to ITERATING with an explanation.

## Your part and the agent's part

- **You**: approve the plan, review the local diff, and advance out of ITERATING when it's
  good to merge.
- **The agent**: plans, implements, commits, and, once you approve, merges the branch, gets
  it pushed back to your repo, and advances to complete.

## Pushing back to your repo

The agent works in a throwaway per-task clone (under `~/.local/share/panopticon/tasks/`),
not in your repo. A merge made there is worthless until it reaches the repo you actually
work in — so MERGING ends with a push, and the `local-merged` responsibility isn't met until
that push succeeds.

**The session service performs the push, not the container.** The clone's `origin` is your
repo's `git_url`, which for a local repo is a path on the host — a path that doesn't exist
inside the container. So the agent asks (the `request_push` MCP tool) and the session
service, which runs where the clone lives, does the git and records the outcome back onto
the task.

**Two branches go, in this order:**

1. `panopticon/<slug>`, the task branch — a backup;
2. your base branch (detected from `origin/HEAD`, never assumed to be `main`).

The order is deliberate. Git refuses a push to the branch your repo currently has **checked
out** (see below), and that's only ever the base branch — the task branch is a new ref, so it
always lands. Whatever happens to the base-branch push, the commits reach your repo and you
can merge them yourself with `git merge panopticon/<slug>`.

### Why git refuses a push to your checked-out branch

A push only moves a branch pointer; it doesn't touch files. If something pushed to the branch
your repo has checked out, that branch would jump forward while the files on disk stayed
behind — and `git status` would then report every arriving commit as an uncommitted change
*reverting* it. Git blocks that by default rather than hand you the footgun. The setting is
`receive.denyCurrentBranch`:

- **`refuse`** (the default) — rejects the push.
- **`updateInstead`** — accepts it **and** updates your files to match, exactly as if you'd
  run `git pull`. It still refuses while you have uncommitted changes, so it can't clobber
  work in progress.

Panopticon arranges this for you: **`panopticon quickstart` sets `updateInstead`
automatically** on a local repo it registers (running the command inside the repo is you
adopting it), and the **[`setup-repo`](setup-repo.md)** flow offers to set it for repos
registered another way. Either only ever sets an unset value — if you've chosen something
yourself, it's left alone.

If neither ran, nothing breaks: the base-branch push is refused, the task branch still lands,
and the agent relays the fix (`git config receive.denyCurrentBranch updateInstead`) along with
the `git merge panopticon/<slug>` alternative.

This workflow is built for repos whose `origin` is a **local path**. The session service holds
no credentials for a networked remote, so pointing it at one gets you a clear error rather than
a push — use [`github-self-reviewed`](github-self-reviewed.md) there instead.

## Skills

- **`local-merge`** detects the base branch, merges the task branch into it with a merge
  commit, and requests the push. On conflicts or a refused push it returns the task to
  ITERATING with the reason; on success it advances to complete.

There's no `gh` tool and no PR/CI plumbing. That's the point of this workflow.

## Related

- [`github-self-reviewed`](github-self-reviewed.md): the same self-review model, but ships
  a GitHub PR.
- [Workflow catalog](README.md).
