# Base task-container image (ADR 0005 base layer). Workflow- and repo-specific layers stack
# on top later. Holds enough to run the entrypoint and the agent: Python, git, bash (the tmux
# pane's shell), the panopticon package, and the `claude` CLI the agent launcher execs. `gh`
# arrives in a later slice.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install --yes --no-install-recommends git bash curl ca-certificates \
 && rm --recursive --force /var/lib/apt/lists/*

# The Claude Code CLI (the agent runtime). `container/agent.py` execs `claude`, so it must be on
# PATH; the installer drops it under ~/.local/bin, which we symlink onto a PATH dir. Lives in the
# base layer for now (every task uses it); M3's multi-CLI support may move it to an agent layer.
RUN curl --fail --silent --show-error --location https://claude.ai/install.sh | bash \
 && ln --symbolic /root/.local/bin/claude /usr/local/bin/claude

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The runner spawns this detached; it connects back to the task service (PANOPTICON_* env),
# registers for liveness, runs the slug hook, and heartbeats until stopped.
ENTRYPOINT ["python", "-m", "panopticon.container"]
