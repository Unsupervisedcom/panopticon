# Base task-container image (ADR 0005 base layer, ADR 0014 §4 per-CLI variant). Workflow- and
# repo-specific layers stack on top later. Holds enough to run the entrypoint and the agent: Python,
# git, bash (the tmux pane's shell), the panopticon package, and the agent CLI the launcher execs.
# `gh` arrives via the github-peer-reviewed workflow layer.
#
# The agent CLI is selected at build time by the `AGENT_CLI` build arg (`claude` — the default — or
# `codex`), so `panopticon-base-claude` and `panopticon-base-codex` are genuinely different images
# from one Dockerfile (the runner passes `--build-arg AGENT_CLI=<cli>`, ADR 0014 §4). Only the
# selected CLI is installed.
#
# The task runs **unprivileged**: a baked `panopticon` user that the entrypoint remaps to the
# invoking host user's uid/gid at start (so /workspace files are host-owned), then drops to via
# gosu. claude is installed under that user's home so it's readable + self-manageable by it.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install --yes --no-install-recommends git bash curl ca-certificates gosu passwd vim \
 && rm --recursive --force /var/lib/apt/lists/*

# The unprivileged account the task runs as. The entrypoint remaps it to the invoking uid/gid at
# start, so the baked default (1000) only matters until then. Its home is where claude installs.
RUN groupadd --gid 1000 panopticon \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/panopticon --shell /bin/bash panopticon

ENV HOME=/home/panopticon
ENV PATH=/home/panopticon/.local/bin:$PATH

# Which agent CLI to install (ADR 0014 §4). `claude` (default) or `codex`; only the selected one is
# installed, so each `panopticon-base-<cli>` variant carries exactly its own runtime.
ARG AGENT_CLI=claude
# The pinned codex release — the single one the codex adapter's config/auth/model behavior is
# verified against (mirrors the reference harness's CODEX_VERSION). Bump here to move the pin.
ARG CODEX_VERSION=0.144.4

# Claude Code CLI (the agent runtime). Installed AS `panopticon` so it lands under its home
# (~/.local) — readable/executable by the unprivileged user and self-manageable (`claude /doctor`
# and updates expect it at $HOME/.local/bin). On PATH via the ENV above. Only when AGENT_CLI=claude.
USER panopticon
RUN if [ "$AGENT_CLI" = "claude" ]; then \
      curl --fail --silent --show-error --location https://claude.ai/install.sh | bash; \
    fi
USER root

# Codex CLI (the agent runtime). The statically-linked musl binary straight from GitHub releases —
# no node/npm needed. Installed as root to /usr/local/bin (0755, world-executable) so the dropped
# `panopticon` user can exec it; on PATH by default. Only when AGENT_CLI=codex.
RUN if [ "$AGENT_CLI" = "codex" ]; then \
      set -eux; \
      arch="$(uname -m)"; \
      case "$arch" in \
        x86_64) triple="x86_64-unknown-linux-musl" ;; \
        aarch64) triple="aarch64-unknown-linux-musl" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
      esac; \
      curl --fail --silent --show-error --location \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-$triple.tar.gz" \
        | tar --extract --gzip --directory /usr/local/bin; \
      if [ -e "/usr/local/bin/codex-$triple" ]; then mv "/usr/local/bin/codex-$triple" /usr/local/bin/codex; fi; \
      chmod 0755 /usr/local/bin/codex; \
    fi

ARG PANOPTICON_VERSION=""
ARG PANOPTICON_WHEEL=""
# Conditional install: supply PANOPTICON_WHEEL (filename in the build context) for dev/CI builds
# where the package isn't yet on PyPI; supply PANOPTICON_VERSION for production pip installs.
RUN --mount=type=bind,source=.,target=/ctx \
    if [ -n "${PANOPTICON_WHEEL}" ]; then \
      pip install --no-cache-dir "/ctx/${PANOPTICON_WHEEL}"; \
    else \
      pip install --no-cache-dir "panopticon-app==${PANOPTICON_VERSION}"; \
    fi

COPY entrypoint.sh /usr/local/bin/panopticon-entrypoint
RUN chmod 0755 /usr/local/bin/panopticon-entrypoint

# Start as root so the entrypoint can adopt the invoking uid/gid; it then drops to `panopticon` and
# execs the command — the task entrypoint below (connect/register/heartbeat), or `login`'s claude.
ENTRYPOINT ["/usr/local/bin/panopticon-entrypoint"]
CMD ["python", "-m", "panopticon.container"]
