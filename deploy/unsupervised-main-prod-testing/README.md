# Enable panopticon agents to do finder prod-testing

Give unsupervised-main task agents the ability to **provision test pods, generate the finder
executable, and profile results** in prod — the workflow that's currently done by hand.

Approach (operator-chosen): **build the finder binary in a k8s pod (no Docker-in-Docker)**, from a
**pre-baked Harbor builder image**, with the repro ServiceAccount **scoped to a dedicated namespace**.

## What already exists
- Repo env-file injects `PROD_REPRO_KUBECONFIG_B64` (SA `panopticon-repro`, embedded token — no
  `awscli` needed) and `PROD_READONLY_KUBECONFIG_B64`.
- The `panopticon-repro` SA already has pods create/delete/exec/log (in `default`).
- Harbor push creds already exist in CI (`vars.HARBOR_USERNAME` + `secrets.HARBOR_PASSWORD`); the
  in-cluster pull secret `unsupervised-regcred` already exists.

## What this bundle adds

| File | Goes to | Purpose |
|---|---|---|
| `finder-repro-rbac.yaml` | prod cluster (`kubectl apply`) | Dedicated `finder-repro` ns + SA + Role (pods CRUD/exec/log) + ResourceQuota/LimitRange ceiling |
| `unsupervised-main/Dockerfile.finder-builder` | unsupervised-main (PR) | Pre-baked builder image: heavy deps (Rust ext + py deps) installed, source overlaid per build |
| `unsupervised-main/publish.finder-builder.yml` | unsupervised-main (PR) | CI job that builds & pushes `harbor…/images/finder-builder` (reuses existing Harbor creds) |
| `build-finder-in-pod.sh` | panopticon image layer (`/usr/local/bin`) | Agent-run: overlay current src into a builder pod, `pyinstaller`, copy binary out |
| `image-layer.head.dockerfile` | panopticon `$CONFIG/layers/` | **⚠ pending — see below.** kubectl + auto-wiring wrapper |
| `apply.sh` | operator runs once | **⚠ pending — see below.** wire config + scope the SA |

## ⚠ Two credential-handling files still to write (need your OK)
The auto-mode classifier blocked writing files that **decode your prod kubeconfig secret**, which is
correct — they touch a credential. They are:
1. **`image-layer.head.dockerfile`** — installs `kubectl` and a wrapper that lazily materializes
   `~/.kube/config` from `$PROD_REPRO_KUBECONFIG_B64` on first use (works under `bash -c`; no host-hook
   change; the existing `neutralize-claude-hooks.sh` stays as-is).
2. **`apply.sh`** — one-shot operator script: assemble the layer file (head + `build-finder-in-pod.sh`
   as a heredoc) into `$CONFIG/layers/unsupervised-main.dockerfile`; `PATCH /repos/unsupervised-main`
   with `image_layer_file`; **regenerate `PROD_REPRO_KUBECONFIG_B64`** to point at the `finder-repro`
   namespace (backing up the old value first); rebuild the composed image.

Approve those and I'll write them.

## Apply sequence (once everything's written)
1. **PR** `Dockerfile.finder-builder` + `publish.finder-builder.yml` into unsupervised-main; run the CI
   job once → `harbor…/images/finder-builder:latest` exists.
2. `kubectl --context <prod> apply -f finder-repro-rbac.yaml`  (creates `finder-repro`).
3. Copy the pull secret into the new ns (no new account):
   `kubectl --context <prod> -n default get secret unsupervised-regcred -o yaml \
      | sed 's/namespace: default/namespace: finder-repro/' \
      | kubectl --context <prod> -n finder-repro apply -f -`
4. `./apply.sh`  (wires the layer + repo config; **rewrites the kubeconfig secret to finder-repro** —
   the one prod-credential mutation, done with a backup).
5. Smoke test: `build-finder-in-pod.sh` produces a binary; provision a repro pod in `finder-repro`;
   profile via `kubectl exec` (passive cgroup `cpu.stat`).

## Profiling
No extra agent-side capability: it's `kubectl exec` into the test pod (cgroup `cpu.stat` +
table-completion progress; py-spy in-pod). See the finder-load perf notes.

## Rebuild cadence
The pre-baked image only needs rebuilding when the **heavy deps** change (requirements.txt / Rust ext /
python-utils) — ordinary `fc.py` edits are overlaid at build-in-pod time, so day-to-day this is free.
