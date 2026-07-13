"""The SetupRepo workflow is a valid shell workflow: a single RUNNING state that advances to
COMPLETE, run as a host shell script rather than a task container."""

from __future__ import annotations

import importlib.resources
import shlex
import stat
import subprocess
from pathlib import Path

from panopticon.core import Actor
from panopticon.core.workflow import Workflow
from panopticon.workflows import SetupRepo

WF = SetupRepo()

# The sourceable helpers (extract_oauth_token / store_oauth_token) the functional tests exercise in a
# real `sh`, no LLM — the token is a literal fixture, `claude`/`script` are never invoked.
_LIB = (importlib.resources.files("panopticon.workflows") / "setup_repo_lib.sh").read_text()


def _sh(body: str) -> str:
    """Run ``body`` after the helpers in a POSIX shell; return its stdout."""
    result = subprocess.run(
        ["sh", "-c", f"{_LIB}\n{body}"], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_default_workflow_runner_type_is_docker() -> None:
    # The base default keeps every existing workflow on the container backend.
    assert Workflow.runner_type == "docker"


def test_setup_repo_is_a_shell_workflow() -> None:
    assert WF.runner_type == "shell"
    assert WF.opt_in is True  # an operator utility, hidden from the picker unless enabled


def test_setup_repo_needs_no_clone_and_no_workdir_override() -> None:
    # It mints a token, so it doesn't touch repo code — runs in an empty task dir at the default spot.
    assert WF.clone_repo is False
    assert WF.shell_workdir is None


def test_starts_running_with_user_turn() -> None:
    task = WF.start_task("t1", "r1", at="2026-07-11T00:00:00Z")
    assert task.state == "RUNNING"
    assert task.turn is Actor.USER  # initial state
    assert task.workflow == "setup-repo"


def test_running_advances_to_complete() -> None:
    # The single non-DROPPED edge → `advance` derives → COMPLETE (what the script POSTs on success).
    assert WF.operations("RUNNING").get("advance") == "COMPLETE"
    assert set(WF.transitions("RUNNING")) == {"COMPLETE", "DROPPED"}


def test_running_has_no_responsibilities() -> None:
    # A shell task runs no agent, so there are no agent obligations gating the advance.
    assert list(WF.responsibilities("RUNNING")) == []


def test_shell_script_runs_setup_repo_and_advances() -> None:
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
    # the return-to-dashboard hint is echoed up front, before the credential check / any prompts.
    # (The sourceable helpers are prepended and mention CLAUDE_CODE_OAUTH_TOKEN in their bodies, so
    # anchor on the interactive flow's credential *check* — `${CLAUDE_CODE_OAUTH_TOKEN:-}` — which
    # only the flow contains.)
    assert 'echo "$dashboard_hint"' in script
    assert script.index('echo "$dashboard_hint"') < script.index("${CLAUDE_CODE_OAUTH_TOKEN:-}")


def test_shell_script_captures_and_writes_the_minted_token() -> None:
    script = WF.shell_script()
    # captures the interactive `claude setup-token` in a pty so its output can be read back
    assert "script -q -e -c 'claude setup-token'" in script
    # extracts the minted token and stores it in the repo's env-file via the helpers
    assert "extract_oauth_token" in script
    assert "store_oauth_token" in script and "PANOPTICON_ENV_FILE" in script
    # comments out an existing active token, and drops a placeholder comment stub
    assert "# CLAUDE_CODE_OAUTH_TOKEN=" in script  # the sed replacement that comments it out
    assert "grep -vE" in script  # the filter that removes the placeholder stub
    # still falls back to on-screen copy guidance when it can't capture/write
    assert "Copy the token shown above into" in script


def test_shell_script_converges_on_a_summary_and_completes_on_a_final_enter() -> None:
    script = WF.shell_script()
    # every route ends with a summary + a complete-on-Enter prompt
    assert "Summary:" in script
    assert "Press Enter to complete this task and return to the dashboard" in script
    # the completion (panopticon_advance) is the final action — after the credential-check branches,
    # run on any route — not gated on `claude setup-token` succeeding
    assert script.rindex("panopticon_advance") > script.rindex("claude setup-token")


def test_extract_oauth_token_pulls_the_token_out_of_a_noisy_capture() -> None:
    # A real `claude setup-token` capture is wrapped in ANSI colour codes and other chatter; the
    # helper still recovers the sk-ant-oat01-… token (and the last one, if the flow reprints it).
    out = _sh(
        "cap=$(mktemp); "
        "printf 'noise\\n\\033[1msk-ant-oat01-STALE\\033[0m done\\n"
        'your token: \\033[32msk-ant-oat01-Fresh_Tok-123\\033[0m\\n\' > "$cap"; '
        'extract_oauth_token "$cap"; rm -f "$cap"'
    )
    assert out.strip() == "sk-ant-oat01-Fresh_Tok-123"


def test_store_oauth_token_creates_a_private_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "secrets" / "repo.env"  # parent dir does not exist yet
    _sh(f"store_oauth_token sk-ant-oat01-NEW {shlex.quote(str(env_file))}")
    assert env_file.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW\n"
    # holds a live credential — created private (0600)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_store_oauth_token_comments_out_the_old_token_and_drops_the_stub(tmp_path: Path) -> None:
    env_file = tmp_path / "repo.env"
    env_file.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"
        "ANTHROPIC_API_KEY=key-123\n"
        "# CLAUDE_CODE_OAUTH_TOKEN =\n"  # a placeholder stub to be removed
        "# a note we keep\n"
    )
    _sh(f"store_oauth_token sk-ant-oat01-NEW {shlex.quote(str(env_file))}")
    lines = env_file.read_text().splitlines()

    # the previous active token is preserved, but commented out (deactivated, not deleted)
    assert "# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD" in lines
    # the placeholder comment stub is gone
    assert "# CLAUDE_CODE_OAUTH_TOKEN =" not in lines
    # unrelated secrets and comments are untouched
    assert "ANTHROPIC_API_KEY=key-123" in lines
    assert "# a note we keep" in lines
    # exactly one *active* token line, and it's the new one
    assert [ln for ln in lines if ln.startswith("CLAUDE_CODE_OAUTH_TOKEN=")] == [
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW"
    ]


def test_store_oauth_token_keeps_an_already_commented_out_token(tmp_path: Path) -> None:
    # A real (valued) token that's already commented out is a historical record, not a stub — it must
    # survive a subsequent mint (only empty/placeholder stubs are pruned).
    env_file = tmp_path / "repo.env"
    env_file.write_text("# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ARCHIVED\n")
    _sh(f"store_oauth_token sk-ant-oat01-NEW {shlex.quote(str(env_file))}")
    lines = env_file.read_text().splitlines()
    assert "# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ARCHIVED" in lines
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEW" in lines


def test_docker_workflows_have_no_shell_script_and_default_knobs() -> None:
    from panopticon.workflows import Spike

    spike = Spike()
    assert spike.shell_script() == ""  # the base default; only shell workflows override it
    assert spike.clone_repo is False  # the base defaults; a docker task clones regardless
    assert spike.shell_workdir is None
