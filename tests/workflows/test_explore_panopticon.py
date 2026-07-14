"""The ExplorePanopticon workflow is a valid shell workflow: a single RUNNING state that advances to
COMPLETE, run as a host shell script (clone panopticon into a self-cleaning temp dir + open `claude`)
rather than a task container."""

from __future__ import annotations

import subprocess

from panopticon.core import Actor
from panopticon.workflows import ExplorePanopticon

WF = ExplorePanopticon()


def test_explore_panopticon_is_a_hidden_shell_workflow() -> None:
    assert WF.name == "explore-panopticon"
    assert WF.runner_type == "shell"
    # opt-out (available for every repo) but hidden from both dashboard menus — it's launched from
    # the repos modal's explore hotkey, not the pickers.
    assert WF.opt_in is False
    assert WF.hidden is True


def test_explore_panopticon_needs_no_clone_and_no_workdir_override() -> None:
    # The script does its own clone into a temp dir, so the task dir stays empty and there's no
    # workflow-level clone or workdir override.
    assert WF.clone_repo is False
    assert WF.shell_workdir is None


def test_starts_running_with_user_turn() -> None:
    task = WF.start_task("t1", "r1", at="2026-07-14T00:00:00Z")
    assert task.state == "RUNNING"
    assert task.turn is Actor.USER  # initial state
    assert task.workflow == "explore-panopticon"


def test_running_advances_to_complete() -> None:
    # The single non-DROPPED edge → `advance` derives → COMPLETE (what the script POSTs when done).
    assert WF.operations("RUNNING").get("advance") == "COMPLETE"
    assert set(WF.transitions("RUNNING")) == {"COMPLETE", "DROPPED"}


def test_running_has_no_responsibilities() -> None:
    # A shell task runs no agent, so there are no agent obligations gating the advance.
    assert list(WF.responsibilities("RUNNING")) == []


def test_shell_script_clones_the_public_remote() -> None:
    script = WF.shell_script()
    # the canonical remote is injected as REPO_URL and cloned (a packaged install has no local
    # checkout, so we always clone the public repo)
    assert "REPO_URL=" in script and "github.com/Unsupervisedcom/panopticon" in script
    assert 'git clone --quiet "$REPO_URL"' in script


def test_shell_script_pins_the_running_version() -> None:
    script = WF.shell_script()
    # resolves the running version and checks out its tag, falling back to the default branch
    assert "panopticon.__version__" in script
    assert 'checkout --quiet "v$version"' in script
    assert "default branch" in script  # the fallback message when there's no matching tag


def test_shell_script_uses_a_self_cleaning_temp_dir() -> None:
    script = WF.shell_script()
    # clone into a mktemp -d dir removed by a trap on exit — "automatically cleaned up"
    assert "mktemp -d" in script
    assert "trap " in script and 'rm -rf "$tmp"' in script


def test_shell_script_opens_claude_as_a_read_only_guide() -> None:
    script = WF.shell_script()
    # launches interactive claude with a guide framing (a read-only tour, pointed at the docs)
    assert 'claude --append-system-prompt "$guide_prompt"' in script
    assert "AGENTS.md" in script and "docs/design/" in script
    assert "do not modify" in script  # read-only tour


def test_shell_script_shows_the_dashboard_detach_hint() -> None:
    script = WF.shell_script()
    # detects/falls back to the tmux detach binding so the operator can get back to the dashboard
    assert "detach-client" in script and "show-options -gv prefix" in script
    assert "return to the dashboard" in script


def test_shell_script_completes_the_task_when_claude_exits() -> None:
    script = WF.shell_script()
    # completes via the panopticon shell lib (loaded by the shell runner), not raw curl, after claude
    assert "panopticon_advance" in script
    assert script.rindex("panopticon_advance") > script.index("claude --append-system-prompt")


def test_shell_script_is_valid_posix_sh() -> None:
    # It's real sh — parse it (with the REPO_URL prefix shell_script adds) without executing, so a
    # syntax error is caught. `git`/`claude`/`python` are never actually invoked.
    result = subprocess.run(["sh", "-n", "-c", WF.shell_script()], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
