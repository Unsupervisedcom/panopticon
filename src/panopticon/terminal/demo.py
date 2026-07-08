"""panopticon demo — register a throwaway local git repo and create two spike tasks.

Running :func:`run_demo` populates the dashboard with two ungated spike tasks so
the operator can watch ≥ 2 agents work at once without a GitHub account or token.
No forge dependency: the tasks use the :class:`~panopticon.workflows.Spike` workflow
and point at a self-contained local git repo created in a tempdir.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient

#: Workflow used for demo tasks — forge-free, no planning gate.
_DEMO_WORKFLOW = "spike"


def _init_sample_repo() -> Path:
    """Create a minimal git repo in a tempdir and return its path."""
    tmpdir = Path(tempfile.mkdtemp(prefix="panopticon-demo-"))
    (tmpdir / "README.md").write_text(
        "# panopticon demo\n\nA throwaway repo created by `panopticon demo`.\n"
    )
    (tmpdir / "hello.py").write_text('print("hello from panopticon demo")\n')
    subprocess.run(
        ["git", "-C", str(tmpdir), "init", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmpdir), "add", "--all"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmpdir), "commit",
         "--message=Initial demo commit",
         "--author=panopticon demo <demo@panopticon.local>"],
        check=True,
        capture_output=True,
    )
    return tmpdir


def run_demo(
    service_url: str,
    *,
    client: TaskServiceClient | None = None,
    repo_path: Path | None = None,
) -> None:
    """Register a sample repo and create two spike tasks against it.

    ``client`` and ``repo_path`` are injected in tests; callers that omit them get a
    real HTTP client pointed at ``service_url`` and a freshly generated git repo.
    """
    http = httpx.Client(base_url=service_url)
    client = client or TaskServiceClient(http)
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

    print(f"Demo ready — repo '{repo_id}', tasks: {', '.join(task_ids)}")
    print("Open the dashboard (`make start`) to watch ≥ 2 agents work at once.")
