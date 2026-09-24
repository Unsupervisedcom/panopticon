# Prod-testing approval gate (agent instruction)

> Add to unsupervised-main's agent instructions (AGENTS.md / CLAUDE.md). In the interim
> (broad role, pods in `default`) this turn-handoff is the **only** guardrail — there is no
> namespace ResourceQuota to fall back on — so it is not optional.

You can build a modified finder binary and run it in a prod test pod (`build-finder-in-pod.sh`,
`kubectl`). Interim: these pods run in the **`default` namespace** as `unsupervised-unsupervised`,
which has **broad production access** (read+write via the main-app IRSA role) and runs **alongside
real prod workloads with no resource quota**. Treat this as a high-privilege, high-blast-radius
action.

## When to use it
Only when a change's correctness or performance **can only be shown empirically** and a unit test
can't — a data-scale bug repro or a load/finding perf number. Strongly prefer a cheap **synthetic
input** that isolates the mechanism (no prod data, no big pod) whenever it suffices.

## MUST: pause for operator approval before ANY prod pod
Before you `kubectl run`/`apply` any pod, **end your turn and hand it to the operator** with:
- what change / which binary,
- **pod size (cpu + memory) and expected wall-clock** — there is no quota clamp, so this is your
  only bound; keep it as small as the test allows,
- **which prod exports it will read** (S3 URIs / run ids),
- what it measures and the pass/fail criterion.

Do not create the pod until the operator advances the turn back approving it. A denial means don't
run it.

## Rules while running
- Namespace `default` only; do not touch, delete, or exec into pods you did not create.
- Label every pod you create `app.kubernetes.io/managed-by=panopticon` so it's identifiable.
- **Always delete your test pods the moment you're done** — nothing else will reclaim them.
- Read-only intent: you are validating a change, not mutating prod. Do not write to prod buckets
  or trigger re-exports; if a test needs fresh/other data, ask the operator to stage the export.
