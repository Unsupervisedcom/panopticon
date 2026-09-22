#!/usr/bin/env bash
# Build the finder PyInstaller binary in a k8s pod (NO Docker-in-Docker).
#
# Baked into the panopticon repo image layer at /usr/local/bin. Uses the pre-baked Harbor builder
# image, which carries ONLY toolchain + deps + the compiled bfinder wheel (no finder/Rust source).
# This script overlays the agent's CURRENT finder source, recompiles the Rust wheel *iff* the Rust
# source changed (auto-detected via RUST_SRC_HASH — a Python-only change never recompiles; a Rust
# change is never tested stale), installs finder editable, and runs pyinstaller. kubectl is
# auto-wired to `default` (interim) by the image-layer wrapper; the builder pod pulls via
# unsupervised-regcred.
#
# Usage: build-finder-in-pod.sh [--src DIR] [--out FILE] [--ns NS] [--image IMG]
#                               [--name POD] [--force-rust] [--keep]
set -euo pipefail

SRC="subrepos/finder"
OUT="./finder"
# Interim: default ns (broad prod-unsupervised-main IRSA). Phase 2: NS=finder-repro.
NS="default"
IMAGE="${FINDER_BUILDER_IMAGE:-harbor.unsupervised.com/images/finder-builder:latest}"
POD=""
FORCE_RUST=0
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --ns) NS="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --name) POD="$2"; shift 2;;
    --force-rust) FORCE_RUST=1; shift;;   # recompile the wheel even if the hash matches
    --keep) KEEP=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[ -d "$SRC/src" ] || { echo "no finder src at $SRC/src (run from the repo root, or pass --src)" >&2; exit 2; }
if [ -z "$POD" ]; then
  h="$(find "$SRC/src" -type f -printf '%P %s\n' 2>/dev/null | cksum | cut -d' ' -f1)"
  POD="finder-build-${h}"
fi

cleanup() { [ "$KEEP" = 1 ] || kubectl -n "$NS" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== launching builder pod $POD (image=$IMAGE, ns=$NS) =="
kubectl -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl -n "$NS" apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  labels: { app.kubernetes.io/managed-by: panopticon, purpose: finder-build }
spec:
  restartPolicy: Never
  imagePullSecrets: [{ name: unsupervised-regcred }]
  containers:
    - name: builder
      image: ${IMAGE}
      command: ["sleep", "3600"]
      resources:
        requests: { cpu: "4", memory: 6Gi, ephemeral-storage: 12Gi }
        limits:   { cpu: "8", memory: 12Gi, ephemeral-storage: 20Gi }
YAML

echo "== waiting for Ready =="
kubectl -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=300s
kubectl -n "$NS" exec "$POD" -- test -f /build/FINDER_BUILDER_READY \
  || { echo "pod is not a finder-builder image (missing /build/FINDER_BUILDER_READY)" >&2; exit 3; }

echo "== overlaying current finder source into the pod (transient — deleted with the pod) =="
tar -C "$SRC" --exclude=venv --exclude=.venv --exclude=target --exclude=dist \
    --exclude=__pycache__ --exclude=.git -czf - . \
  | kubectl -n "$NS" exec -i "$POD" -- bash -c 'mkdir -p /build/subrepos/finder && tar -C /build/subrepos/finder -xzf -'

echo "== recompiling the bfinder wheel only if the Rust source changed =="
kubectl -n "$NS" exec "$POD" -- bash -lc "
  set -euo pipefail
  cd /build/subrepos/finder
  rust_hash() { find Cargo.toml Cargo.lock bfinder pybfinder -type f | sort | xargs sha256sum | sha256sum | cut -d' ' -f1; }
  baked=\$(cat /build/RUST_SRC_HASH)
  now=\$(rust_hash)
  if [ '${FORCE_RUST}' = '1' ] || [ \"\$now\" != \"\$baked\" ]; then
    echo '   Rust source changed (or --force-rust) -> recompiling wheel'
    (cd pybfinder && maturin build -r && pip install --force-reinstall --no-deps target/wheels/*.whl)
  else
    echo '   Rust unchanged -> using baked wheel (fast path)'
  fi
  echo '== installing finder (editable) from overlaid src + pyinstaller =='
  pip install -e . --no-deps
  sh pyinstaller.sh
"

echo "== copying binary out -> $OUT =="
kubectl -n "$NS" cp "$POD:/build/subrepos/finder/dist/finder" "$OUT"
chmod +x "$OUT"
echo "== done: $OUT ($(wc -c < "$OUT") bytes). linux/amd64 binary — run it in a pod, not on the agent host. =="
