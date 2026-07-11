"""The SetupToken workflow is a valid shell workflow: a single RUNNING state that advances to
COMPLETE, run as a host shell script rather than a task container."""

from __future__ import annotations

from panopticon.core import Actor
from panopticon.core.workflow import Workflow
from panopticon.workflows import SetupToken

WF = SetupToken()


def test_default_workflow_runner_type_is_docker() -> None:
    # The base default keeps every existing workflow on the container backend.
    assert Workflow.runner_type == "docker"


def test_setup_token_is_a_shell_workflow() -> None:
    assert WF.runner_type == "shell"
    assert WF.opt_in is True  # an operator utility, hidden from the picker unless enabled


def test_starts_running_with_user_turn() -> None:
    task = WF.start_task("t1", "r1", at="2026-07-11T00:00:00Z")
    assert task.state == "RUNNING"
    assert task.turn is Actor.USER  # initial state
    assert task.workflow == "setup-token"


def test_running_advances_to_complete() -> None:
    # The single non-DROPPED edge → `advance` derives → COMPLETE (what the script POSTs on success).
    assert WF.operations("RUNNING").get("advance") == "COMPLETE"
    assert set(WF.transitions("RUNNING")) == {"COMPLETE", "DROPPED"}


def test_running_has_no_responsibilities() -> None:
    # A shell task runs no agent, so there are no agent obligations gating the advance.
    assert list(WF.responsibilities("RUNNING")) == []


def test_shell_script_runs_setup_token_and_advances() -> None:
    script = WF.shell_script()
    assert "claude setup-token" in script
    # drives its own lifecycle over REST using the env vars the shell runner injects
    assert "$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/operations/advance" in script


def test_docker_workflows_have_no_shell_script() -> None:
    from panopticon.workflows import Spike

    assert Spike().shell_script() == ""  # the base default; only shell workflows override it
