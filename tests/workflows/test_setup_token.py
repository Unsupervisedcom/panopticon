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


def test_setup_token_needs_no_clone_and_no_workdir_override() -> None:
    # It mints a token, so it doesn't touch repo code — runs in an empty task dir at the default spot.
    assert WF.clone_repo is False
    assert WF.shell_workdir is None


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
    # completes the task via the panopticon shell lib (loaded by the shell runner), not raw curl
    assert "panopticon_advance" in script


def test_shell_script_checks_for_an_existing_credential_and_guides_the_operator() -> None:
    script = WF.shell_script()
    # branches on an already-configured credential (env-file sourced by the shell runner)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in script and "ANTHROPIC_API_KEY" in script
    assert "$PANOPTICON_ENV_FILE" in script or "PANOPTICON_ENV_FILE" in script  # names the env-file
    # tells the operator they can drop the task instead (dashboard 'x')
    assert "'x'" in script and "drop" in script.lower()
    # detects/falls back to the tmux detach binding to get back to the dashboard
    assert "detach-client" in script and "show-options -gv prefix" in script


def test_shell_script_shows_the_dashboard_hint_first() -> None:
    script = WF.shell_script()
    # the return-to-dashboard hint is echoed up front, before the credential check / any prompts
    assert 'echo "$dashboard_hint"' in script
    assert script.index('echo "$dashboard_hint"') < script.index("CLAUDE_CODE_OAUTH_TOKEN")


def test_shell_script_converges_on_a_summary_and_completes_on_a_final_enter() -> None:
    script = WF.shell_script()
    # every route ends with a summary + a complete-on-Enter prompt
    assert "Summary:" in script
    assert "Press Enter to complete this task and return to the dashboard" in script
    # the completion (panopticon_advance) is the final action — after the credential-check branches,
    # run on any route — not gated on `claude setup-token` succeeding
    assert script.rindex("panopticon_advance") > script.rindex("claude setup-token")


def test_docker_workflows_have_no_shell_script_and_default_knobs() -> None:
    from panopticon.workflows import Spike

    spike = Spike()
    assert spike.shell_script() == ""  # the base default; only shell workflows override it
    assert spike.clone_repo is False  # the base defaults; a docker task clones regardless
    assert spike.shell_workdir is None
