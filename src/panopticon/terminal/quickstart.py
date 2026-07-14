"""First-time setup helpers for ``panopticon quickstart``.

Registers the repo quickstart is run in with the running task service (idempotent — deduped on
the remote URL), writes a secrets template to ``~/.config/panopticon/panopticon.env`` when it
doesn't already exist, and creates a ``setup-repo`` task the console attaches to on open so the
operator mints their Claude auth token as the last first-time-setup step.
"""

from __future__ import annotations

import httpx

from panopticon.client import TaskServiceClient
from panopticon.terminal.setup_repo_task import SETUP_REPO_WORKFLOW, create_setup_repo_task

_FALLBACK_GIT_URL = "https://github.com/Unsupervisedcom/panopticon.git"

#: Task states past which a setup-repo task is done — used to decide whether to reuse one.
_TERMINAL_STATES = {"COMPLETE", "DROPPED"}


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


def _find_existing_repo(client: TaskServiceClient, git_url: str) -> dict[str, object] | None:
    """The already-registered repo for ``git_url``, matched by normalized remote **or** derived id.

    Deduping on the remote URL alone misses a repo registered under a different spelling of the
    same remote (e.g. ``git@github.com:acme/x.git`` vs ``https://github.com/acme/x``), and since the
    id we'd create is derived from the URL, that mismatch would then collide on create. Matching the
    derived id too reuses the existing repo instead of colliding.
    """
    target = _normalize_url(git_url)
    want_id = repo_id_from_url(git_url)
    for repo in client.list_repos():
        if _normalize_url(str(repo.get("git_url", ""))) == target or repo.get("id") == want_id:
            return repo
    return None


def setup_repo(client: TaskServiceClient, git_url: str, env_file: str) -> tuple[str, str]:
    """Register the repo quickstart is run in with the task service; return its ``(id, name)``.

    Idempotent: an already-registered repo (matched by remote URL or derived id, see
    :func:`_find_existing_repo`) is reused rather than re-registered, and a create that races into a
    conflict falls back to reuse. The name is used to seed the setup-repo task's memo.
    """
    existing = _find_existing_repo(client, git_url)
    if existing is not None:
        print(f"Repo already configured for {git_url!r} — skipping registration.")
        repo_id = str(existing["id"])
        return repo_id, str(existing.get("name") or repo_id)
    repo_id = repo_id_from_url(git_url)
    try:
        client.create_repo(repo_id, repo_id, git_url, env_file=env_file)
    except httpx.HTTPStatusError as err:
        if err.response.status_code != 409:
            raise
        # A repo with this id already exists (its remote just didn't match our dedup) — reuse it.
        print(f"Repo {repo_id!r} already exists — reusing it.")
        return repo_id, repo_id
    print(f"Registered repo {repo_id!r} (git_url={git_url!r}).")
    print(f"  → Secrets file: {env_file}")
    return repo_id, repo_id


def ensure_setup_repo_task(client: TaskServiceClient, repo_id: str, name: str) -> str | None:
    """Return the id of a ``setup-repo`` task for ``repo_id`` to attach to, creating one if needed.

    Reuses an existing **non-terminal** setup-repo task for the repo when there is one, so
    re-running quickstart doesn't pile up orphaned ``RUNNING`` tasks; otherwise creates a fresh one
    (seeded with the shared memo, see :func:`create_setup_repo_task`). The console attaches to the
    returned task on open, dropping the operator into ``claude setup-token``. Best-effort: if the
    task can't be created (e.g. the workflow isn't available on an older task service), it warns and
    returns ``None`` so quickstart still opens the console.
    """
    try:
        for task in client.list_tasks():
            if (
                task.get("repo_id") == repo_id
                and task.get("workflow") == SETUP_REPO_WORKFLOW
                and task.get("state") not in _TERMINAL_STATES
            ):
                print("Attaching to the running setup-repo task to mint a Claude auth token.")
                return str(task["id"])
        task = create_setup_repo_task(client, repo_id, name)
    except httpx.HTTPError as err:
        print(f"Could not start a setup-repo task ({err}); opening the dashboard instead.")
        print("  → Mint a token later with `claude setup-token`, or start a setup-repo task.")
        return None
    print("Minting a Claude auth token — attach to complete `claude setup-token`.")
    return str(task["id"])
