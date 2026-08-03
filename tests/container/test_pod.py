"""The pod bootstrap: cloning the workspace and starting the in-pod agent session. No git, no tmux —
the command runner is a fake that records calls. LLM-free (the agent it starts is not run here)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from panopticon.container import pod


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> None:
        self.calls.append(list(args))


def _env(**overrides: str) -> dict[str, str]:
    return {
        "PANOPTICON_CONTAINER_ID": "panopticon-t1",
        "PANOPTICON_TASK_ID": "t1",
        **overrides,
    }


def test_bootstrap_clones_the_repo_then_starts_the_agent_session(tmp_path: Path) -> None:
    rec = _Recorder()

    pod.bootstrap(
        _env(
            PANOPTICON_GIT_URL="https://forge/repo.git", PANOPTICON_WORKSPACE=str(tmp_path / "ws")
        ),
        run=rec,
    )

    clone, session = rec.calls
    assert clone == ["git", "clone", "https://forge/repo.git", str(tmp_path / "ws")]
    assert session[:6] == ["tmux", "new-session", "-d", "-s", "panopticon-t1", "-c"]
    assert session[7:] == ["python", "-m", "panopticon.container.agent"]


def test_the_session_is_named_like_the_job_so_kubectl_exec_can_find_it(tmp_path: Path) -> None:
    rec = _Recorder()
    pod.bootstrap(_env(PANOPTICON_WORKSPACE=str(tmp_path)), run=rec)
    assert rec.calls[-1][4] == "panopticon-t1"


def test_an_existing_checkout_is_not_re_cloned_over(tmp_path: Path) -> None:
    """A pod restart lands on a populated volume; re-cloning would destroy the agent's work."""
    (tmp_path / ".git").mkdir()
    rec = _Recorder()

    cloned = pod.clone_workspace("https://forge/repo.git", tmp_path, run=rec)

    assert cloned is False
    assert rec.calls == []


def test_a_task_without_a_git_url_still_gets_an_agent(tmp_path: Path) -> None:
    """Better an attachable task the operator can direct than a pod that exits before anything is
    attachable."""
    rec = _Recorder()

    pod.bootstrap(_env(PANOPTICON_WORKSPACE=str(tmp_path)), run=rec)

    assert [call[0] for call in rec.calls] == ["tmux"]
