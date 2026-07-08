"""panopticon demo — register a throwaway local git repo and create two spike tasks.

Running :func:`run_demo` populates the dashboard with two queued spike tasks so
the operator can see tasks appearing without needing a GitHub account or forge token.
No forge dependency: the tasks use the :class:`~panopticon.workflows.Spike` workflow
and point at a self-contained local git repo seeded from ``examples/sample-repo/``.

Note on agents actually running: a runner must be active (``make start``) and the repo
must have an ``env_file`` set for containers to authenticate. The demo creates the tasks;
operators wire credentials and trigger work from the dashboard.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient

#: Seed content for the throwaway git repo — committed files the operator can browse.
_SAMPLE_REPO_SRC = Path(__file__).parent.parent.parent.parent / "examples" / "sample-repo"

#: Git identity used for the single seed commit; avoids a failure on machines with no
#: global git config (the onboarding target).
_GIT_AUTHOR = "panopticon demo <demo@panopticon.local>"

#: Workflow used for demo tasks — forge-free, no planning gate.
_DEMO_WORKFLOW = "spike"


def _init_sample_repo(*, src: Path | None = None) -> Path:
    """Copy ``src`` into a tempdir, git-init it, and return its path.

    Passes ``user.name`` and ``user.email`` via ``-c`` so the commit succeeds on
    machines with no global git identity configured (fresh-install scenario).
    """
    src = src or _SAMPLE_REPO_SRC
    tmpdir = Path(tempfile.mkdtemp(prefix="panopticon-demo-"))
    shutil.copytree(str(src), str(tmpdir), dirs_exist_ok=True)
    for cmd in (
        ["git", "-C", str(tmpdir), "init", "--initial-branch=main"],
        ["git", "-C", str(tmpdir), "add", "--all"],
        ["git", "-C", str(tmpdir),
         "-c", "user.name=panopticon demo",
         "-c", "user.email=demo@panopticon.local",
         "commit", "--message=Initial demo commit"],
    ):
        subprocess.run(cmd, check=True, capture_output=True)
    return tmpdir


def run_demo(
    service_url: str,
    *,
    client: TaskServiceClient | None = None,
    repo_path: Path | None = None,
) -> None:
    """Register a sample repo and create two spike tasks against it.

    ``client`` and ``repo_path`` are injected in tests; callers that omit them get a
    real HTTP client pointed at ``service_url`` and a freshly generated git repo seeded
    from ``examples/sample-repo/``.
    """
    http: httpx.Client | None = None
    if client is None:
        http = httpx.Client(base_url=service_url)
        client = TaskServiceClient(http)
    repo_path = repo_path or _init_sample_repo()

    repo_id = f"demo-{uuid.uuid4().hex[:8]}"
    client.create_repo(repo_id, "panopticon demo", str(repo_path))

    task_ids = []
    for i in (1, 2):
        t = client.create_task(
            repo_id,
            _DEMO_WORKFLOW,
            memo=f"Demo task {i} — open-ended agent sandbox",
        )
        task_ids.append(t["id"])

    if http is not None:
        http.close()

    print(f"Demo ready — repo '{repo_id}', tasks: {', '.join(task_ids)}")
    print(
        "Open the dashboard (`make start`) to see the tasks. "
        "Add an env-file to the repo (dashboard `g` → patch) for agents to authenticate."
    )
