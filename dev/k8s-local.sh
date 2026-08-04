#!/usr/bin/env bash
# Bring the ADR 0014 Kubernetes backend up against a local agent-operator dev cluster.
#
# The control plane stays on this machine; only task pods run in the cluster. This script does the
# three things that are fiddly by hand — ship the task image into the cluster, check the Agent is
# reconciled, and derive the address a pod uses to call back — then prints the two commands to run.
#
# Usage:  dev/k8s-local.sh [--agent researcher]
#
# Assumes agent-operator's dev cluster is already up:
#   cd ~/repos/ai-outfitter/agent-operator
#   devenv processes up -d cluster && devenv tasks run operator:install
set -euo pipefail

AGENT="panopticon"
OPERATOR_REPO="${OPERATOR_REPO:-$HOME/repos/ai-outfitter/agent-operator}"
IMAGE="${IMAGE:-panopticon-base}"
PORT="${PORT:-8000}"

while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SHARE="$OPERATOR_REPO/.devenv/state/link-cluster/shared"
export KUBECONFIG="$SHARE/kubeconfig"

if [ ! -s "$KUBECONFIG" ]; then
  echo "no dev-cluster kubeconfig at $KUBECONFIG — start the cluster first:" >&2
  echo "  cd $OPERATOR_REPO && devenv processes up -d cluster" >&2
  exit 1
fi
kubectl get --raw=/readyz >/dev/null

# The Agent is the whole configuration: its namespace, service account, quota and credentials are
# what a task Job runs inside. Apply the checked-in dev Agent when it is not there yet.
if ! kubectl get agents.link.aioutfitter.com "$AGENT" >/dev/null 2>&1; then
  if [ "$AGENT" = "panopticon" ]; then
    echo "applying the dev Agent…"
    kubectl apply -f "$(dirname "$0")/k8s-agent.yaml"
    for _ in $(seq 1 60); do
      kubectl get namespace "agent-$AGENT" >/dev/null 2>&1 && break
      sleep 1
    done
  else
    echo "no Agent named '$AGENT' — apply one (see $OPERATOR_REPO/config/samples)" >&2
    exit 1
  fi
fi
NAMESPACE="$(kubectl get agents.link.aioutfitter.com "$AGENT" -o jsonpath='{.status.namespace}')"
if [ -z "$NAMESPACE" ]; then
  echo "Agent '$AGENT' has no status.namespace yet — the operator has not reconciled it" >&2
  exit 1
fi

# The agent's credentials are the task's credentials — that is the point of running as an Agent, so
# the token is put in its namespace rather than passed at spawn. Seeded from the environment or from
# the operator's own claude credentials so the local loop has a working agent.
if ! kubectl -n "$NAMESPACE" get secret panopticon-credentials >/dev/null 2>&1; then
  TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
  if [ -z "$TOKEN" ]; then
    echo "no CLAUDE_CODE_OAUTH_TOKEN in the environment — creating an empty credentials secret." >&2
    echo "The pod will start and be attachable, but the agent will not authenticate." >&2
  fi
  kubectl -n "$NAMESPACE" create secret generic panopticon-credentials \
    --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# The microVM imports any *.tar dropped in its shared images dir (a 5s timer), so shipping the image
# is a save + a wait for the import stamp. imagePullPolicy is IfNotPresent, so the imported copy is
# what the pod runs — there is no registry in this cluster.
# The pod's image must be a reference the *cluster* resolves. A bare "panopticon-base" is expanded
# by containerd to docker.io/library/panopticon-base, which does not exist — so the image is tagged
# under localhost/ before it is shipped, and that fully-qualified name is what the runner is told.
CLUSTER_IMAGE="localhost/panopticon-base:dev"
echo "shipping $IMAGE into the cluster as $CLUSTER_IMAGE…"
mkdir -p "$SHARE/images"
docker tag "$IMAGE:latest" "$CLUSTER_IMAGE"
# Write beside the target and move into place: an archive is written whole (podman refuses to
# modify one), and the guest's import timer must never see a half-written tar.
rm -f "$SHARE/images/panopticon-base.tar.tmp"
docker save "$CLUSTER_IMAGE" -o "$SHARE/images/panopticon-base.tar.tmp"
mv -f "$SHARE/images/panopticon-base.tar.tmp" "$SHARE/images/panopticon-base.tar"
for _ in $(seq 1 60); do
  if [ -s "$SHARE/imported/panopticon-base.tar.sha256" ] && \
     [ "$(cat "$SHARE/imported/panopticon-base.tar.sha256")" = "$(sha256sum "$SHARE/images/panopticon-base.tar" | cut -d' ' -f1)" ]; then
    break
  fi
  sleep 2
done

# QEMU user-mode networking: the guest reaches this host at the slirp gateway, so that — not
# localhost — is where a task pod finds the task service.
POD_SERVICE_URL="http://10.0.2.2:$PORT"

cat <<EOF

cluster    $(kubectl config current-context)  ($KUBECONFIG)
agent      $AGENT → namespace $NAMESPACE
image      $CLUSTER_IMAGE (imported)
pods call  $POD_SERVICE_URL

Run these in two terminals, from this checkout:

  1. the control plane, with the demo kubernetes workflow registered

     KUBECONFIG=$KUBECONFIG \\
       uv run python -m panopticon.taskservice --workflows-path dev/workflows

  2. the host daemon, with the Kubernetes backend enabled

     KUBECONFIG=$KUBECONFIG \\
       uv run python -m panopticon.sessionservice.host \\
         --kubernetes --kubernetes-service-url $POD_SERVICE_URL \\
         --kubernetes-image $CLUSTER_IMAGE

Then create a task on the 'k8s-spike' workflow (dashboard 'n', or the REST API) and watch it:

  KUBECONFIG=$KUBECONFIG kubectl -n $NAMESPACE get jobs,pods -w
EOF
