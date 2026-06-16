"""Per-task host workspaces (ADR 0011): the `repo` symlink the session service swaps base→worktree.
Real-filesystem tests (tmp_path) — no Docker, no LLM."""

from __future__ import annotations

import os
from pathlib import Path

from panopticon.sessionservice.workspace import TaskWorkspaces


def test_prepare_points_repo_at_the_base_and_makes_the_config_dir(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    ws = TaskWorkspaces(str(tmp_path / "tasks"))

    repo = ws.prepare("t1", base=str(base))

    assert repo == ws.repo_link("t1")
    assert os.path.realpath(repo) == str(base)  # repo → base checkout
    assert Path(ws.config_dir("t1")).is_dir()  # per-task agent config dir exists (ADR 0011 §5)


def test_repoint_swaps_repo_to_the_worktree(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ws = TaskWorkspaces(str(tmp_path / "tasks"))
    ws.prepare("t1", base=str(base))

    ws.repoint("t1", str(worktree))

    assert os.readlink(ws.repo_link("t1")) == str(worktree)  # now the writable worktree
    assert os.path.realpath(ws.repo_link("t1")) == str(worktree)


def test_repoint_is_idempotent_and_leaves_no_temp_link(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ws = TaskWorkspaces(str(tmp_path / "tasks"))
    ws.prepare("t1", base=str(worktree))  # start already pointing where we'll repoint

    ws.repoint("t1", str(worktree))  # no-op path
    ws.repoint("t1", str(worktree))  # and again

    assert os.readlink(ws.repo_link("t1")) == str(worktree)
    assert not Path(f"{ws.repo_link('t1')}.tmp").exists()  # the atomic-swap temp is cleaned up


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    base1 = tmp_path / "base1"
    base1.mkdir()
    base2 = tmp_path / "base2"
    base2.mkdir()
    ws = TaskWorkspaces(str(tmp_path / "tasks"))

    ws.prepare("t1", base=str(base1))
    ws.prepare("t1", base=str(base2))  # re-prepare just re-points

    assert os.readlink(ws.repo_link("t1")) == str(base2)
