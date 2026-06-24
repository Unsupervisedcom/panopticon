#!/usr/bin/env bash
# panopticon task-container entrypoint: adopt the invoking user's uid/gid, then drop privileges.
#
# The runner passes PANOPTICON_PUID / PANOPTICON_PGID (the host user that invoked it). We start as
# root, remap the baked-in `panopticon` account to those ids so files the agent writes to the
# bind-mounted /workspace are host-owned (git then sees matching ownership — no "dubious ownership"),
# make its home + the /creds volume writable by it, then `exec` the real command as that unprivileged
# user via gosu. LLM-free — no agent runs here.
set -euo pipefail

puid="${PANOPTICON_PUID:-1000}"
pgid="${PANOPTICON_PGID:-1000}"
cur_uid="$(id --user panopticon)"
cur_gid="$(id --group panopticon)"

# Remap the baked `panopticon` account to the invoking ids (a no-op when they already match the
# baked 1000:1000 default — e.g. a typical Linux host).
#
# `--non-unique` on the group is load-bearing: the host gid may already belong to another group in
# the base image, so a plain `groupmod --gid` onto it fails ("GID already exists", exit 4). That is
# exactly what crashed the container on macOS, whose primary gid 20 (`staff`) collides with
# Debian's `dialout`. Letting `panopticon` *share* the gid is harmless — file ownership is by gid
# number, so /workspace files still land on the host gid — and it covers every host: macOS gid 20,
# a Linux user in gid 100 (`users`), or any other already-taken gid.
#
# The gid is remapped independently of the uid: a host whose uid matches the baked default but
# whose gid does not (uid 1000 + gid 100 is common on Linux) must still be remapped, and re-owning
# $HOME is gated on *either* id moving (it was created under 1000:1000 at build time).
if [ "$cur_gid" != "$pgid" ]; then
    groupmod --non-unique --gid "$pgid" panopticon
fi
if [ "$cur_uid" != "$puid" ]; then
    usermod --uid "$puid" panopticon
fi
if [ "$cur_uid" != "$puid" ] || [ "$cur_gid" != "$pgid" ]; then
    chown --recursive "$puid:$pgid" /home/panopticon
fi
# Hand the whole creds volume to the adopted user so claude can read/refresh its OAuth token.
# Recursive on purpose: the *files* must be owned too (a fresh volume is root-owned, and creds
# written by an earlier root/other-uid `login` would otherwise be unreadable — the unprivileged
# user can't read a root-owned 0600 .credentials.json, so claude would prompt to log in every
# container). Best-effort: /creds may be absent (a task with no creds volume).
chown --recursive "$puid:$pgid" /creds 2>/dev/null || true
# Same for the per-task config volume at the agent's config dir (claude's history lives here): a
# fresh volume is root-owned, and one written by a different uid before would be unreadable.
# Best-effort — it may not be a mount (a task without the config volume).
chown --recursive "$puid:$pgid" /home/panopticon/.claude 2>/dev/null || true

# docker_in_docker capability (ADR-0005 repo capability): a privileged container running a nested
# Docker daemon. dockerd needs root, so start it here — before we drop privileges — and put the
# adopted user in the `docker` group so it can reach the socket. Requires the image to ship the
# Docker engine (the repo's image_layer); we warn and carry on if it doesn't.
if [ "${PANOPTICON_DOCKER_IN_DOCKER:-0}" = "1" ]; then
    if command -v dockerd >/dev/null 2>&1; then
        groupadd --force docker
        usermod --append --groups docker panopticon
        dockerd >/var/log/dockerd.log 2>&1 &
        for _ in $(seq 1 50); do [ -S /var/run/docker.sock ] && break; sleep 0.2; done
    else
        echo "PANOPTICON_DOCKER_IN_DOCKER=1 but dockerd is not installed (add it in the repo's image_layer)" >&2
    fi
fi

exec gosu panopticon "$@"
