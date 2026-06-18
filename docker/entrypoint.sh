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

# Remap `panopticon` to the invoking ids (a no-op when they already match the baked default).
if [ "$(id --group panopticon)" != "$pgid" ]; then
    groupmod --gid "$pgid" panopticon
fi
if [ "$(id --user panopticon)" != "$puid" ]; then
    usermod --uid "$puid" --gid "$pgid" panopticon
    chown --recursive "$puid:$pgid" /home/panopticon
fi
# Hand the whole creds volume to the adopted user so claude can read/refresh its OAuth token.
# Recursive on purpose: the *files* must be owned too (a fresh volume is root-owned, and creds
# written by an earlier root/other-uid `login` would otherwise be unreadable — the unprivileged
# user can't read a root-owned 0600 .credentials.json, so claude would prompt to log in every
# container). Best-effort: /creds may be absent (a task with no creds volume).
chown --recursive "$puid:$pgid" /creds 2>/dev/null || true

exec gosu panopticon "$@"
