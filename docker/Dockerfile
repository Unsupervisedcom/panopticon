# Base task-container image (ADR 0005 base layer). Workflow- and repo-specific layers stack on top
# later. Holds enough to run the entrypoint and the agent: Python, git, bash (the tmux pane's
# shell), the panopticon package, and the `claude` CLI the agent launcher execs. `gh` arrives via
# the github-peer-reviewed workflow layer.
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

# Claude Code CLI (the agent runtime). Installed AS `panopticon` so it lands under its home
# (~/.local) — readable/executable by the unprivileged user and self-manageable (`claude /doctor`
# and updates expect it at $HOME/.local/bin). On PATH via the ENV above.
USER panopticon
RUN curl --fail --silent --show-error --location https://claude.ai/install.sh | bash
USER root

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Mountpoint for a repo's OAuth creds volume (ADR 0007); the entrypoint chowns it to the adopted uid.
RUN mkdir --parents /creds

COPY docker/entrypoint.sh /usr/local/bin/panopticon-entrypoint
RUN chmod 0755 /usr/local/bin/panopticon-entrypoint

# Start as root so the entrypoint can adopt the invoking uid/gid; it then drops to `panopticon` and
# execs the command — the task entrypoint below (connect/register/heartbeat), or `login`'s claude.
ENTRYPOINT ["/usr/local/bin/panopticon-entrypoint"]
CMD ["python", "-m", "panopticon.container"]
