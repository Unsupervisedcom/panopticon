#!/usr/bin/env bash
# panopticon task-container entrypoint: adopt the invoking user's uid/gid, then drop privileges.
#
# The runner passes PANOPTICON_PUID / PANOPTICON_PGID (the host user that invoked it). We start as
# root, remap the baked-in `panopticon` account to those ids so files the agent writes to the
# bind-mounted /workspace are host-owned (git then sees matching ownership — no "dubious ownership"),
# make its home writable by it, then `exec` the real command as that unprivileged
# user via gosu. LLM-free — no agent runs here.
set -euo pipefail

puid="${PANOPTICON_PUID:-1000}"
pgid="${PANOPTICON_PGID:-1000}"

# Remap `panopticon` to the invoking ids (a no-op when they already match the baked default).
# If the target gid/uid is already owned by a different principal, delete it first — groups
# like `dialout` (gid 20 on Debian, same as macOS `staff`) are irrelevant in an agent container.
if [ "$(id --group panopticon)" != "$pgid" ]; then
    existing_group=$(getent group "$pgid" | cut --delimiter=: --fields=1 || true)
    if [ -n "$existing_group" ] && [ "$existing_group" != "panopticon" ]; then
        groupdel "$existing_group"
    fi
    groupmod --gid "$pgid" panopticon
fi
if [ "$(id --user panopticon)" != "$puid" ]; then
    existing_user=$(getent passwd "$puid" | cut --delimiter=: --fields=1 || true)
    if [ -n "$existing_user" ] && [ "$existing_user" != "panopticon" ]; then
        userdel "$existing_user"
    fi
    usermod --uid "$puid" --gid "$pgid" panopticon
    chown --recursive "$puid:$pgid" /home/panopticon
fi
# Hand the per-task config volume at the agent's config dir (claude's history lives here) to the
# adopted user: a
# fresh volume is root-owned, and one written by a different uid before would be unreadable.
# Best-effort — it may not be a mount (a task without the config volume).
chown --recursive "$puid:$pgid" /home/panopticon/.claude 2>/dev/null || true

# Signal that the remap + both chowns are DONE, so the runner can create the agent pane without
# racing them. The runner's readiness probe used to infer this from `stat`-ing the config mount's
# root, but a `chown --recursive` sets the root before it finishes descending — the probe passed
# mid-chown, the pane exec'd early, and the agent died writing its skills
# (PermissionError on <config>/commands), taking the tmux session with it and putting the heal loop
# into a respawn storm. A sentinel is exact where a heuristic wasn't.
#
# Deliberately under /run (container-local, gone on restart), NOT the per-task config volume, which
# persists across respawns — a stale marker there would make every later spawn "ready" instantly.
mkdir --parents /run
: >/run/panopticon-entrypoint-ready

# docker_in_docker capability (ADR-0005 repo capability): a privileged container running a nested
# Docker daemon. dockerd needs root, so start it here — before we drop privileges — and put the
# adopted user in the `docker` group so it can reach the socket. Requires the image to ship the
# Docker engine (the repo's image_layer); we warn and carry on if it doesn't.
if [ "${PANOPTICON_DOCKER_IN_DOCKER:-0}" = "1" ]; then
    if command -v dockerd >/dev/null 2>&1; then
        groupadd --force docker
        usermod --append --groups docker panopticon
        mkdir --parents /var/log
        dockerd >/var/log/dockerd.log 2>&1 &
        for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
        if ! docker info >/dev/null 2>&1; then
            echo "ERROR: dockerd failed to start within 30 s. Last 50 lines of /var/log/dockerd.log:" >&2
            tail --lines=50 /var/log/dockerd.log >&2 || true
            exit 1
        fi
        export DOCKER_HOST=unix:///var/run/docker.sock
    else
        echo "PANOPTICON_DOCKER_IN_DOCKER=1 but dockerd is not installed (add it in the repo's image_layer)" >&2
    fi
fi

exec gosu panopticon "$@"
