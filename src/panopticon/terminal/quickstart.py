"""First-time setup helpers for ``panopticon quickstart``.

Registers the panopticon repo with the running task service (idempotent), and writes a secrets
template to ``~/.config/panopticon/panopticon.env`` when it doesn't already exist.
"""

from __future__ import annotations

import httpx

from panopticon.client import TaskServiceClient

PANOPTICON_REPO_ID = "panopticon"
_FALLBACK_GIT_URL = "https://github.com/Unsupervisedcom/panopticon.git"
_SECRETS_TEMPLATE = """\
# Panopticon agent secrets
# Fill in before creating tasks. This file is injected into task containers as environment variables.
#
# Claude authentication — one of:
CLAUDE_CODE_OAUTH_TOKEN=
# ANTHROPIC_API_KEY=
#
# GitHub access token (for creating/reading PRs, issues, etc.)
GH_TOKEN=
"""


def detect_git_url() -> str:
    """Return the git remote URL for origin in CWD, or the known GitHub fallback."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _FALLBACK_GIT_URL


def ensure_secrets_file() -> str:
    """Write the secrets template to ~/.config/panopticon/panopticon.env if absent.

    Returns the absolute path to the file (created or pre-existing).
    """
    from panopticon.core.dirs import user_config_dir

    secrets_path = user_config_dir() / "panopticon.env"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    if secrets_path.exists():
        print(f"Secrets file already exists: {secrets_path}")
    else:
        secrets_path.write_text(_SECRETS_TEMPLATE)
        print(f"Created secrets template: {secrets_path}")
        print("  → Edit it to add your CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN before creating tasks.")
    return str(secrets_path)


def wait_for_service(service_url: str, *, timeout: int = 30) -> None:
    """Poll the task service until it responds or ``timeout`` seconds elapse."""
    import time

    import httpx as _httpx

    deadline = time.monotonic() + timeout
    while True:
        try:
            _httpx.get(f"{service_url}/tasks", timeout=1.0).raise_for_status()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Task service at {service_url} did not respond within {timeout}s"
                )
            time.sleep(1.0)


def setup_panopticon_repo(client: TaskServiceClient, git_url: str, env_file: str) -> None:
    """Register the panopticon repo with the task service.

    Idempotent: prints a message and returns immediately when the repo already exists.
    """
    try:
        client.get_repo(PANOPTICON_REPO_ID)
        print("Panopticon repo already configured — skipping registration.")
        return
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    client.create_repo(PANOPTICON_REPO_ID, "panopticon", git_url, env_file=env_file)
    print(f"Registered panopticon repo (git_url={git_url!r}).")
    print(f"  → Secrets file: {env_file}")
