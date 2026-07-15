# `setup-repo`

A host-side **setup utility**, not a coding workflow. Today it mints a repo's `claude`
auth token by running `claude setup-token` on the host. It's a **shell** workflow: it runs
in a host tmux session with **no container and no agent** (no LLM involved).

```
RUNNING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** run a repo's host-side setup in a shell. Today that's minting a Claude
auth token via `claude setup-token`. Attach to complete the interactive flow, and the
token lands in the repo's env-file.

## How you launch it

This workflow is **hidden** from the normal task-creation picker. You start it from the
**repos screen's setup hotkey**, which creates a `setup-repo` task for the highlighted
repo. (It's available for every repo; there's nothing to enable.)

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **RUNNING** | The session service runs the setup script in a host tmux session. Attach with `t`; the script checks for an existing credential, optionally collects a new one, and prompts you to finish. | **The script**: a final Enter completes the task; or **drop** it to keep an existing credential. |
| **COMPLETE** | Terminal. The token is in the repo's env-file. | n/a |

There's no plan, no container image, no per-task clone, and no responsibilities. A shell
task has no agent to gate.

## Your part and the script's part

- **You**: attach to the session, complete (or skip) the browser OAuth flow, and press
  Enter to finish, or drop the task if you'd rather add your own token by hand.
- **The script**: detects an existing credential, guides you through `claude setup-token`,
  writes the token to the repo's env-file, and completes the task.

## Related

- [Container authentication](../container-auth.md): what the token is for and how to set
  it by hand instead.
- [Workflow catalogue](README.md).
