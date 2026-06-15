# Base task-container image (ADR 0005 base layer). Workflow- and repo-specific layers stack
# on top later. Holds just enough to run the entrypoint: Python, git, bash (the tmux pane's
# shell), and the panopticon package. `gh`/`claude` arrive in later slices.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The runner spawns this detached; it connects back to the task service (PANOPTICON_* env),
# registers for liveness, runs the slug hook, and heartbeats until stopped.
ENTRYPOINT ["python", "-m", "panopticon.container"]
