# Repo image layer (ADR 0005) for unsupervised-main — finder prod-testing tooling.
#
# This is a Dockerfile *fragment*, not a full Dockerfile: sessionservice/images.py composes
# `FROM <base>\n<workflow layer>\n<this>` into one image, and the build context is a temp dir
# holding ONLY the generated Dockerfile — so there is NO COPY. Extra files are inlined via RUN.
# apply.sh appends build-finder-in-pod.sh (as a heredoc) after this head and installs the result
# to $PANOPTICON_CONFIG/layers/unsupervised-main.dockerfile.
#
# The base ends on `USER root` and ships curl + ca-certificates, so kubectl installs cleanly;
# the base entrypoint drops back to the `panopticon` user at runtime.

USER root

# --- kubectl (real binary), arch-matched to the container ---
ARG KUBECTL_VERSION=v1.30.5
RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl.real \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl.real \
 && /usr/local/bin/kubectl.real version --client=true --output=yaml >/dev/null

# --- kubectl wrapper: lazily materialize ~/.kube/config from the injected
#     PROD_REPRO_KUBECONFIG_B64 env var on first use. Works under `bash -c`
#     (Claude's Bash tool) — no login-shell / profile.d dependency. The env var is
#     injected by the runner from the repo env-file; it is never baked into the image. ---
RUN cat > /usr/local/bin/kubectl <<'WRAP' && chmod 0755 /usr/local/bin/kubectl
#!/usr/bin/env bash
set -euo pipefail
cfg="${KUBECONFIG:-$HOME/.kube/config}"
if [ ! -s "$cfg" ] && [ -n "${PROD_REPRO_KUBECONFIG_B64:-}" ]; then
  mkdir -p "$(dirname "$cfg")"
  ( umask 077; printf '%s' "$PROD_REPRO_KUBECONFIG_B64" | base64 -d > "$cfg" )
fi
exec /usr/local/bin/kubectl.real "$@"
WRAP
