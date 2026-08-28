# `resident-agent`

Delegate a GitHub change to the repository's configured long-lived resident agent. Panopticon runs
no agent and creates no container: the session service files an issue, assigns it to
`Repo.resident_agent`, and watches GitHub until the resident's pull request is approved and merged.

```text
FILING → IMPLEMENTING → REVIEW → MERGING → COMPLETE
```

(`DROPPED` is reachable from every non-terminal state.)

## Before using it

Enable this opt-in workflow for the repo, set its **resident agent** forge login, and configure an
env-file containing `GH_TOKEN`. The token is used by the host's `gh` CLI; it is never stored in the
task record. An optional `issue.md` task artifact supplies the issue body.

## Lifecycle

| State | Watched fact |
|---|---|
| **FILING** | The issue exists, is assigned to the resident, and its URL is on the task. |
| **IMPLEMENTING** | A pull request cross-referencing the issue exists; the task URL changes to the PR. |
| **REVIEW** | The PR is ready, a code-owner review was requested or submitted, and GitHub reports `APPROVED`. Changes requested returns the task to IMPLEMENTING. |
| **MERGING** | The approved PR has merged. |
| **COMPLETE** | Terminal. |

Every transition is performed by the deterministic watcher (`advanced_by = AGENT`). CODEOWNERS on
GitHub chooses reviewers; Panopticon does not. The user can drop the task or direct a free state move.

The task's URL is deliberately overloaded as the durable external pointer: it is the issue while no
PR exists, then the PR. This is how a restarted daemon resumes without local watcher state.

## Issue text

The first memo line is the title. The body is `issue.md` when present, otherwise `initial_prompt`,
otherwise the remaining memo lines. A final searchable `Panopticon task: <task-id>` line makes issue
creation idempotent across a crash between GitHub creation and recording the URL.

See [ADR 0015](../design/decisions/0015-forge-watched-resident-workflows.md) and the
[workflow catalog](README.md).
