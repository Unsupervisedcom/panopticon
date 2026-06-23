"""Re-run the agent's credential bootstrap in an already-running container.

The agent launcher wires a task's auth **once at spawn** (`agent.main()`): it symlinks the
repo's OAuth credential file in from the creds volume and seeds the logged-in account into the
container-local config. So a container that started before `panopticon login` (or before a
re-login) stays unauthenticated until it respawns.

The runner's `login` execs this module (`python -m panopticon.container.refresh_credentials`)
into each running task container of the repo right after writing fresh creds to the volume, so
the new auth state reaches live containers without a restart. It reuses `agent`'s bootstrap
steps verbatim — the single source of truth — against the same config dir the launcher uses
(`CLAUDE_CONFIG_DIR` if set, else `~/.claude`). Idempotent and best-effort, exactly like the
launch-time bootstrap.
"""

from __future__ import annotations

import os
from pathlib import Path

from panopticon.container.agent import CREDS_DIR, link_credentials, seed_account


def main(config_dir: Path | None = None, *, creds_dir: Path = Path(CREDS_DIR)) -> None:
    # Same config dir the launcher uses (CLAUDE_CONFIG_DIR if set, else ~/.claude); both are
    # injectable for tests, defaulting to the in-container paths.
    config_dir = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    link_credentials(config_dir, creds_dir=creds_dir)  # symlink .credentials.json in (if missing)
    seed_account(config_dir, creds_dir=creds_dir)  # (re)seed the account, so the token isn't a prompt


if __name__ == "__main__":  # pragma: no cover - exec'd into the container
    main()
