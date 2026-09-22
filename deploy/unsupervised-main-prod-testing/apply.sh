#!/usr/bin/env bash
# One-shot operator setup to enable unsupervised-main agents' finder prod-testing.
#
#   apply.sh config     # wire the image layer + repo config (idempotent, non-prod)
#   apply.sh scope-sa   # prod: create finder-repro RBAC, mint a scoped kubeconfig,
#                        # and REWRITE the PROD_REPRO_KUBECONFIG_B64 secret (backed up first)
#   apply.sh all        # config then scope-sa
#
# scope-sa is the only step that mutates prod + a credential. It TESTS the new kubeconfig
# (creates+deletes a probe pod in finder-repro) and only overwrites the secret if the test
# passes; the old secret value is backed up to a timestamped file first.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PANOPTICON_CONFIG="${PANOPTICON_CONFIG:-$HOME/.config/panopticon}"
LAYERS_DIR="$PANOPTICON_CONFIG/layers"
SECRETS_DIR="$PANOPTICON_CONFIG/secrets"
ENV_FILE="$SECRETS_DIR/unsupervised_main.env"
SVC="${PANOPTICON_SERVICE_URL:-http://localhost:8000}"
PROD_CTX="${PROD_CTX:-arn:aws:eks:us-east-1:037004398141:cluster/prod}"
NS="finder-repro"
SA="panopticon-repro"
LAYER_NAME="unsupervised-main.dockerfile"

log() { printf '\n== %s ==\n' "$*"; }

config() {
  log "assembling image layer -> $LAYERS_DIR/$LAYER_NAME"
  mkdir -p "$LAYERS_DIR"
  {
    cat "$HERE/image-layer.head.dockerfile"
    printf '\n# --- inlined build-finder-in-pod.sh (build context is Dockerfile-only, so no COPY) ---\n'
    printf "RUN cat > /usr/local/bin/build-finder-in-pod.sh <<'FINDERBUILD' && chmod 0755 /usr/local/bin/build-finder-in-pod.sh\n"
    cat "$HERE/build-finder-in-pod.sh"
    printf '\nFINDERBUILD\n'
  } > "$LAYERS_DIR/$LAYER_NAME"

  log "PATCH repo config: image_layer_file=$LAYER_NAME"
  curl -fsS -X PATCH "$SVC/repos/unsupervised-main" \
    -H 'Content-Type: application/json' \
    -d "{\"image_layer_file\": \"$LAYER_NAME\"}" >/dev/null
  echo "  ok"

  log "forcing image rebuild (drop cached composed tags; runner rebuilds on next spawn)"
  for wf in github-peer-reviewed github-self-reviewed; do
    docker rmi "panopticon-$wf-unsupervised-main" 2>/dev/null || true
  done
  echo "  dropped; a fresh task spawn will compose base -> workflow -> repo layer."
}

scope_sa() {
  log "applying scoped RBAC to prod ($NS)"
  kubectl --context "$PROD_CTX" apply -f "$HERE/finder-repro-rbac.yaml"

  log "copying pull secret unsupervised-regcred into $NS"
  kubectl --context "$PROD_CTX" -n default get secret unsupervised-regcred -o yaml \
    | sed -e 's/^  namespace: default/  namespace: '"$NS"'/' \
          -e '/resourceVersion:/d' -e '/uid:/d' -e '/creationTimestamp:/d' \
    | kubectl --context "$PROD_CTX" -n "$NS" apply -f -

  log "minting a long-lived token secret for $NS/$SA"
  kubectl --context "$PROD_CTX" -n "$NS" apply -f - <<YAML
apiVersion: v1
kind: Secret
metadata:
  name: ${SA}-token
  namespace: ${NS}
  annotations:
    kubernetes.io/service-account.name: ${SA}
type: kubernetes.io/service-account-token
YAML
  # wait for the controller to populate the token
  for _ in $(seq 1 30); do
    tok="$(kubectl --context "$PROD_CTX" -n "$NS" get secret "${SA}-token" -o jsonpath='{.data.token}' 2>/dev/null || true)"
    [ -n "$tok" ] && break; sleep 1
  done
  [ -n "$tok" ] || { echo "token secret never populated" >&2; exit 3; }

  log "building the scoped kubeconfig"
  server="$(kubectl --context "$PROD_CTX" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
  ca="$(kubectl --context "$PROD_CTX" -n "$NS" get secret "${SA}-token" -o jsonpath='{.data.ca\.crt}')"
  token="$(printf '%s' "$tok" | base64 -d)"
  newkc="$(mktemp)"; trap 'rm -f "$newkc"' RETURN
  cat > "$newkc" <<KC
apiVersion: v1
kind: Config
clusters:
- name: prod
  cluster: { server: ${server}, certificate-authority-data: ${ca} }
contexts:
- name: prod-repro
  context: { cluster: prod, namespace: ${NS}, user: ${SA} }
current-context: prod-repro
users:
- name: ${SA}
  user: { token: ${token} }
KC

  log "TESTING the new kubeconfig against $NS (must pass before we touch the secret)"
  kubectl --kubeconfig "$newkc" -n "$NS" auth can-i create pods >/dev/null
  kubectl --kubeconfig "$newkc" -n "$NS" run rbac-probe --image=public.ecr.aws/docker/library/busybox:latest \
    --restart=Never --command -- sh -c 'exit 0' >/dev/null
  kubectl --kubeconfig "$newkc" -n "$NS" delete pod rbac-probe --wait=false >/dev/null 2>&1 || true
  echo "  new kubeconfig works in $NS."

  log "backing up + rewriting PROD_REPRO_KUBECONFIG_B64 in $ENV_FILE"
  cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  newval="$(base64 < "$newkc" | tr -d '\n')"
  # replace just that one line (values may contain '/', so use a python rewrite, not sed)
  ENVFILE="$ENV_FILE" NEWVAL="$newval" python3 - <<'PY'
import os
p=os.environ["ENVFILE"]; nv=os.environ["NEWVAL"]
lines=open(p).read().splitlines(); out=[]; done=False
for ln in lines:
    if ln.startswith("PROD_REPRO_KUBECONFIG_B64="):
        out.append(f"PROD_REPRO_KUBECONFIG_B64={nv}"); done=True
    else: out.append(ln)
assert done, "PROD_REPRO_KUBECONFIG_B64 line not found"
open(p,"w").write("\n".join(out)+"\n")
print("  rewrote PROD_REPRO_KUBECONFIG_B64 (backup kept)")
PY
  echo "  done. New task spawns will use the finder-repro-scoped credential."
}

case "${1:-all}" in
  config)   config ;;
  scope-sa) scope_sa ;;
  all)      config; scope_sa ;;
  *) echo "usage: apply.sh [config|scope-sa|all]" >&2; exit 2 ;;
esac
