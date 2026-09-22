# Enable panopticon agents to do finder prod-testing

Give unsupervised-main task agents the ability to **provision test pods, generate the finder
executable, and profile results** against production data — the workflow currently done by hand.

Two phases:
- **Interim (now, broad role):** test pods run in **`default`** as `unsupervised-unsupervised` (the
  existing `prod-unsupervised-main` IRSA role) to read prod exports. **No cluster/credential/IAM
  changes** — everything needed already exists in `default`. Trade-off: agents get the broad
  (read+write) role and there is **no ResourceQuota** — the operator turn-handoff gate is the only
  guardrail. Files marked "phase 2" below are NOT used here.
- **Phase 2 (later, needs IAM-admin):** scoped `finder-repro` namespace + read-only IRSA SA +
  ResourceQuota. Blocked until an IAM-admin creates the scoped role (the `Unsupervised-Engineer` SSO
  role can't `iam:CreateRole`). `finder-repro-rbac.yaml` + `iam-finder-repro-readonly.json` + `apply.sh
  scope-sa` are the phase-2 path, kept ready.

Common to both: **in-pod finder build (no Docker-in-Docker)** from a pre-baked Harbor builder image,
and every prod run gated by an **operator turn-handoff**.

## Two identities (keep them straight)
- **Control** — `panopticon-repro` SA: creates/execs pods (RBAC). No data access.
- **Run** — `finder-test` SA: the SA the test *pods* run as; IRSA -> read-only prod S3. Data
  access rides here, not on kubectl.

## Files

| File | Goes to | Purpose |
|---|---|---|
| `finder-repro-rbac.yaml` | prod cluster (`kubectl apply`) | `finder-repro` ns + `panopticon-repro` (control) + `finder-test` (run) SAs + Role + ResourceQuota/LimitRange |
| `iam-finder-repro-readonly.json` | **AWS (operator creates)** | IAM role templates: trust (EKS OIDC + `finder-repro:finder-test`) + read-only s3 on `unsupervised-prod-internal/internal/*` |
| `unsupervised-main/Dockerfile.finder-builder` | unsupervised-main (PR) | Pre-baked builder image (deps installed, source overlaid per build) |
| `unsupervised-main/publish.finder-builder.yml` | unsupervised-main (PR) | CI publish job (reuses existing `HARBOR_USERNAME`/`HARBOR_PASSWORD`) |
| `image-layer.head.dockerfile` | `$CONFIG/layers/` (via apply.sh) | kubectl + auto-wiring wrapper (materializes kubeconfig from the injected env var) |
| `build-finder-in-pod.sh` | image layer `/usr/local/bin` | Agent-run in-pod build |
| `repro-pod.template.yaml` | reference | A finder test pod running as `finder-test` (IRSA S3) |
| `prod-testing-gate.md` | unsupervised-main AGENTS.md | The turn-handoff approval rule agents must follow |
| `apply.sh` | operator runs | `config` (wire layer+repo) / `scope-sa` (prod RBAC + rewrite kubeconfig secret) |

## Guardrail = policy + backstop
- **Policy (turn-handoff, `prod-testing-gate.md`):** before any pod in `finder-repro`, the agent
  ends its turn with a proposal (change, pod size, **which exports it reads**, what it measures);
  you approve in the dashboard. Trust-based, per-run, full context.
- **Backstop (enforced by the API server):** `ResourceQuota` caps the namespace (8 pods / 128 CPU
  / 600Gi / 1000Gi scratch), `LimitRange` caps any one pod (64 CPU / 300Gi / 500Gi). IRSA is
  read-only, one bucket. So worst case, even if the policy is ignored, the blast radius is bounded.

## Apply sequence
1. **PR** `Dockerfile.finder-builder` + `publish.finder-builder.yml` into unsupervised-main; run the
   CI job once -> `harbor…/images/finder-builder:latest` exists.
2. **AWS (you):** create IAM role `prod-finder-repro-readonly` from `iam-finder-repro-readonly.json`;
   uncomment the `finder-test` SA's `role-arn` annotation in `finder-repro-rbac.yaml`.
3. `apply.sh config` — assemble+install the image layer, PATCH repo config, force image rebuild.
4. `kubectl --context <prod> apply -f finder-repro-rbac.yaml`; copy the pull secret into the ns:
   `kubectl -n default get secret unsupervised-regcred -o yaml | sed 's/namespace: default/namespace: finder-repro/' | kubectl -n finder-repro apply -f -`
   (or just run `apply.sh scope-sa`, which does the apply + pull-secret copy + kubeconfig rewrite).
5. `apply.sh scope-sa` — mint a `finder-repro`-scoped kubeconfig, **test it (probe pod) before
   overwriting**, back up the old value, rewrite `PROD_REPRO_KUBECONFIG_B64`.
6. Add `prod-testing-gate.md` to unsupervised-main's AGENTS.md.
7. Smoke test: `build-finder-in-pod.sh` -> binary; provision a `repro-pod.template.yaml` pod; profile.

## Rebuild cadence
The pre-baked image only rebuilds when the **heavy deps** change (requirements.txt / Rust ext /
python-utils). Ordinary `fc.py` edits are overlaid at build-in-pod time — day-to-day this is free.
