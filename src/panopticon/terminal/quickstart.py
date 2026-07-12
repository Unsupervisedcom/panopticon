"""First-time setup helpers for ``panopticon quickstart``.

Registers the repo quickstart is run in with the running task service (idempotent — deduped on
the remote URL), and writes a secrets template to ``~/.config/panopticon/panopticon.env`` when it
doesn't already exist.
"""

from __future__ import annotations

from panopticon.client import TaskServiceClient

_FALLBACK_GIT_URL = "https://github.com/Unsupervisedcom/panopticon.git"


def _secrets_template() -> str:
    """The secrets-file template, read from the packaged ``panopticon.env.template`` data file."""
    import importlib.resources

    ref = importlib.resources.files("panopticon.terminal") / "panopticon.env.template"
    return ref.read_text()


def detect_git_url() -> str:
    """Return the git remote URL for origin in CWD, or the panopticon fallback.

    Quickstart adopts whatever repo it's run in; the fallback covers running outside a git
    checkout (or one without an ``origin`` remote).
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        return url or _FALLBACK_GIT_URL
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _FALLBACK_GIT_URL


def _normalize_url(git_url: str) -> str:
    """Canonical form for comparing remote URLs: trimmed, no trailing ``.git`` or ``/``."""
    url = git_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def repo_id_from_url(git_url: str) -> str:
    """Derive a repo id/name from a git URL — its last path segment without the ``.git`` suffix.

    ``https://github.com/Unsupervisedcom/panopticon.git`` → ``panopticon``;
    ``git@github.com:acme/Widget.git`` → ``widget``. Falls back to ``repo`` if the URL yields
    nothing usable.
    """
    tail = _normalize_url(git_url).replace(":", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.lower() or "repo"


def ensure_secrets_file() -> str:
    """Write the secrets template into the secrets dir (~/.config/panopticon/secrets/) if absent.

    Returns the file's **name** relative to the secrets dir (``panopticon.env``) — what a repo's
    ``env_file`` stores, so it resolves against whichever host runs the task (ADR 0007).
    """
    from panopticon.core.dirs import _secrets_dir

    secrets_dir = _secrets_dir()
    secrets_path = secrets_dir / "panopticon.env"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    if secrets_path.exists():
        print(f"Secrets file already exists: {secrets_path}")
    else:
        secrets_path.write_text(_secrets_template())
        print(f"Created secrets template: {secrets_path}")
        print("  → Edit it to add your CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN before creating tasks.")
    return secrets_path.name


def wait_for_service(service_url: str, *, timeout: int = 30) -> None:
    """Poll the task service until it responds or ``timeout`` seconds elapse."""
    import time

    import httpx as _httpx

    deadline = time.monotonic() + timeout
    while True:
        try:
            _httpx.get(f"{service_url}/tasks", timeout=1.0).raise_for_status()
            return
        except Exception as err:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Task service at {service_url} did not respond within {timeout}s"
                ) from err
            time.sleep(1.0)


def setup_repo(client: TaskServiceClient, git_url: str, env_file: str) -> None:
    """Register the repo quickstart is run in with the task service.

    Idempotent: deduped on the remote URL — if any registered repo already points at ``git_url``
    (compared normalized, ignoring a trailing ``.git`` or ``/``), prints a message and returns
    without registering.
    """
    target = _normalize_url(git_url)
    for repo in client.list_repos():
        if _normalize_url(str(repo.get("git_url", ""))) == target:
            print(f"Repo already configured for {git_url!r} — skipping registration.")
            return
    repo_id = repo_id_from_url(git_url)
    client.create_repo(repo_id, repo_id, git_url, env_file=env_file)
    print(f"Registered repo {repo_id!r} (git_url={git_url!r}).")
    print(f"  → Secrets file: {env_file}")
