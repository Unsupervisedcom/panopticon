#!/usr/bin/env bash
# Build the finder PyInstaller binary in a k8s pod (NO Docker-in-Docker).
#
# Baked into the panopticon repo image layer at /usr/local/bin/build-finder-in-pod.sh so
# agents can run it. Uses the pre-baked Harbor builder image (deps already installed); it
# only overlays the agent's CURRENT finder src and runs pyinstaller (~2 min), then copies
# the binary out. kubectl is auto-wired to the finder-repro namespace by the image-layer
# wrapper; the builder pod pulls via the unsupervised-regcred secret (copied into finder-repro).
#
# Usage: build-finder-in-pod.sh [--src DIR] [--out FILE] [--ns NS] [--image IMG]
#                               [--name POD] [--rebuild-rust] [--keep]
set -euo pipefail

SRC="subrepos/finder"
OUT="./finder"
NS="finder-repro"
IMAGE="${FINDER_BUILDER_IMAGE:-harbor.unsupervised.com/images/finder-builder:latest}"
POD=""
REBUILD_RUST=0
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --ns) NS="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --name) POD="$2"; shift 2;;
    --rebuild-rust) REBUILD_RUST=1; shift;;
    --keep) KEEP=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[ -d "$SRC/src" ] || { echo "no finder src at $SRC/src (run from the repo root, or pass --src)" >&2; exit 2; }
# Pod name is deterministic from the src content so concurrent agents don't collide, without
# needing $RANDOM (unavailable in some restricted shells): hash the tree listing.
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

# Sanity: right image?
kubectl -n "$NS" exec "$POD" -- test -f /build/FINDER_BUILDER_READY \
  || { echo "pod is not a finder-builder image (missing /build/FINDER_BUILDER_READY)" >&2; exit 3; }

echo "== syncing current finder src into the pod (overlay editable install) =="
tar -C "$SRC/src" -czf - . | kubectl -n "$NS" exec -i "$POD" -- \
  bash -c 'rm -rf /build/subrepos/finder/src && mkdir -p /build/subrepos/finder/src && tar -C /build/subrepos/finder/src -xzf -'

if [ "$REBUILD_RUST" = 1 ]; then
  echo "== (--rebuild-rust) syncing pybfinder + rebuilding the Rust ext =="
  tar -C "$SRC/pybfinder" -czf - . | kubectl -n "$NS" exec -i "$POD" -- \
    bash -c 'rm -rf /build/subrepos/finder/pybfinder && mkdir -p /build/subrepos/finder/pybfinder && tar -C /build/subrepos/finder/pybfinder -xzf -'
  kubectl -n "$NS" exec "$POD" -- bash -lc 'cd /build/subrepos/finder/pybfinder && maturin build -r && pip install --force-reinstall target/wheels/*.whl'
fi

echo "== running pyinstaller in-pod =="
kubectl -n "$NS" exec "$POD" -- bash -lc 'cd /build/subrepos/finder && sh pyinstaller.sh'

echo "== copying binary out -> $OUT =="
kubectl -n "$NS" cp "$POD:/build/subrepos/finder/dist/finder" "$OUT"
chmod +x "$OUT"
echo "== done: $OUT ($(wc -c < "$OUT") bytes). NOTE: this is a linux/amd64 binary — run it in a pod, not on the agent host. =="
