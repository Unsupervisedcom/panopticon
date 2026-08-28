# 0015 — Forge-watched resident workflows claim without spawning

- Status: Accepted
- Date: 2026-08-28
- Deciders: Nicholas Romero
- Related: ADR 0004 (workflow abstraction), ADR 0008 (execution backends), ADR 0010
  (pull-based observation), ADR 0014 (agent-operator workflows)

## Context

Some repositories already have a long-lived resident agent watching their GitHub notifications.
Panopticon should be able to delegate a change to that resident without starting another agent: file
an issue, assign it, and observe the resulting pull request through code-owner review and merge.
The resident belongs to a forge organization, so its login varies by repository.

ADR 0010 chose pull-based observation over inbound webhooks. The determinism invariant also rules
out an LLM tracker in the control plane: state changes must follow observable forge facts.

## Decision

`runner_type = "forge"` means **claim the task without spawning anything**. There is no container,
tmux session, workspace, or clone. The synthetic execution remains in the existing `awaiting`
lifecycle phase while the session service owns the claim.

`sessionservice/forge_watch.py` polls GitHub through `gh`, behind an injectable command runner. It
uses the repository's configured `GH_TOKEN`, stores durable progress only in the task URL/history/
responsibilities, and advances one idempotent state step per poll. A daemon restart therefore
continues from recorded forge facts rather than replaying an opaque local process.

The resident login is `Repo.resident_agent`, not a workflow constant: a resident belongs to a
repository's forge organization. Panopticon assigns the issue to that login. It does not choose a
reviewer; GitHub's CODEOWNERS rules decide whose review is requested, and the watcher only observes
that a request or review exists.

Merge is tracked, not gated by Panopticon. The human gate is code-owner approval on GitHub; after
approval the watcher observes the merge and completes the task. In this workflow
`advanced_by = AGENT` means the advancing actor is deterministic automation, not necessarily an
LLM-bearing agent.

## Alternatives considered

- **A `runner_type = "shell"` bash poller in tmux.** That puts untested state-machine logic in a
  script, consumes one pane per task for days, and is not restart-safe: `startup_reclaim` correctly
  never reruns shell scripts, so a daemon restart could strand the watch.
- **Webhooks.** They require a reachable authenticated ingress and delivery/replay machinery. ADR
  0010 already chose host-side pull observation for this deployment shape.
- **An LLM agent as tracker.** It spends tokens to interpret facts that GitHub exposes as structured
  data and would violate the control-plane determinism boundary if run in the session service.

## Consequences

- Forge progress is eventually consistent with the configured poll cadence.
- The dashboard displays `awaiting` for the whole watch until a dedicated lifecycle phase exists.
- Each runner needs `gh` and the repo's `GH_TOKEN`; missing credentials block the task without
  attempting a forge call.
- Claims survive daemon restarts because there is no execution resource to recreate.
- The meaning of `Actor.AGENT` includes deterministic actors as well as containerized agents.
