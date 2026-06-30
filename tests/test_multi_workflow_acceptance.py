"""Slice 8 acceptance: the lifecycle is genuinely configurable (the Milestone 1 thesis).

Drives the real task service over REST (via `build_app`): a path-discovered workflow is selectable
with **no core/taskservice change**, and GithubPeerReviewed + the free-form (spike) workflow run concurrently
with **workflow-specific skills**. No Docker, no LLM.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.taskservice.__main__ import build_app

_CUSTOM_WORKFLOW = '''\
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


class Custom(Workflow):
    name = "custom"

    class Only(InitialState):
        label = "ONLY"
        transitions = (Complete,)

    initial = Only
'''


def _skill_names(client: TestClient, task_id: str) -> list[str]:
    resp = client.get(f"/tasks/{task_id}/skills")
    assert resp.status_code == 200, resp.text
    return [s["name"] for s in resp.json()]


def test_multiple_workflows_are_configurable_and_run_concurrently(tmp_path: Path) -> None:
    # A workflow dropped on the discovery path — never referenced by core/taskservice.
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "custom.py").write_text(_CUSTOM_WORKFLOW)

    app = build_app(db="sqlite://", artifacts_root=str(tmp_path / "artifacts"), workflows_path=str(wf_dir))
    with TestClient(app) as client:
        # 1. The built-ins and the path-discovered workflow are all selectable (no core change).
        assert {"spike", "github-peer-reviewed", "custom"} <= {w["name"] for w in client.get("/workflows").json()}

        client.post("/repos", json={"id": "r1", "name": "acme/widgets", "git_url": "https://x/r1.git",
                                    "enabled_workflows": ["github-peer-reviewed"]})

        # 2. Tasks on three different workflows coexist concurrently, each in its own initial state.
        tasks = {
            wf: client.post("/tasks", json={"repo_id": "r1", "workflow": wf}).json()["id"]
            for wf in ("github-peer-reviewed", "spike", "custom")
        }
        states = {wf: client.get(f"/tasks/{tid}").json()["state"] for wf, tid in tasks.items()}
        assert states["spike"] == "ITERATING" and states["custom"] == "ONLY"  # each its workflow's start
        assert states["github-peer-reviewed"] not in ("ITERATING", "ONLY")  # it has its own lifecycle

        # 3. Available skills differ per workflow: github-peer-reviewed carries forge skills; the
        #    free-form one (spike) carries none of them — the lifecycle is the workflow's, not the engine's.
        gpr_skills, spike_skills = _skill_names(client, tasks["github-peer-reviewed"]), _skill_names(client, tasks["spike"])
        assert "open-pr" in gpr_skills
        assert "open-pr" not in spike_skills
        assert gpr_skills != spike_skills
