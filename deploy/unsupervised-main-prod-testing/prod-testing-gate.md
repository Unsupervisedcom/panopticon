# Prod-testing approval gate (agent instruction)

> Add this to unsupervised-main's agent instructions (AGENTS.md / CLAUDE.md) so any task
> agent working the repo sees it. It is the *policy* half of the guardrail; the
> `finder-repro` ResourceQuota/LimitRange is the *technical* backstop.

You have the ability to build a modified finder binary and run it in a prod test pod
(`build-finder-in-pod.sh`, `kubectl` scoped to the `finder-repro` namespace). This reads
and runs against **production data**. Treat it as a privileged action.

## When to use it
Only when a change's correctness or performance **can only be shown empirically** and a unit
test can't — e.g. reproducing a data-scale bug (the >2 GB string overflow shape) or measuring
a load/finding perf number. Prefer a cheap **synthetic input** that isolates the mechanism over
pulling a full prod export whenever that suffices.

## MUST: pause for operator approval before any prod pod
Before you `kubectl run`/`apply` **any** pod in `finder-repro`, **end your turn and hand it to
the operator** with a concrete proposal:
- what change / which binary,
- pod size (cpu + memory) and expected wall-clock,
- **which prod exports it will read** (the S3 URIs / run ids),
- what it measures and the pass/fail criterion.

Do not create the pod until the operator advances the turn back to you approving it. A denial
means don't run it. This is the same review surface as a plan review.

## Bounds you operate within (enforced regardless)
- Namespace `finder-repro` only; ResourceQuota caps totals (8 pods / 128 CPU / 600Gi / 1000Gi
  scratch) and LimitRange caps any single pod (64 CPU / 300Gi / 500Gi). The API server rejects
  anything over.
- Data access is **read-only** on `unsupervised-prod-internal` via the `finder-test` SA. You
  cannot write prod data or re-export from the warehouse — if a test needs fresh/other data,
  ask the operator to stage the export; do not attempt to generate it yourself.
- Always delete your test pods when done.
