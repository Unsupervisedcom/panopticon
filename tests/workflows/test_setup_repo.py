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
    # opt-out (enabled for every repo by default) but hidden from both dashboard menus — it's
    # launched from the repos modal's setup hotkey, not the pickers.
    assert WF.opt_in is False
    assert WF.hidden is True


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


def test_shell_script_offers_the_github_token_when_missing() -> None:
    script = WF.shell_script()
    # the offer step is defined and run before the final summary converges
    assert "maybe_offer_github_token" in script
    # gated on: repo on GitHub (PANOPTICON_GIT_URL), a GH_TOKEN in the env, and it not already in file
    assert "PANOPTICON_GIT_URL" in script
    assert "is_github_url" in script
    assert "GH_TOKEN" in script
    assert "env_file_has_var GH_TOKEN" in script
    # writes it via the helper
    assert "append_env_var GH_TOKEN" in script
    # the offer runs after the Claude credential step (the call — last occurrence of the name —
    # comes after the credential check) but before the final summary
    assert script.rindex("maybe_offer_github_token") > script.index("${CLAUDE_CODE_OAUTH_TOKEN:-}")
    assert script.rindex("maybe_offer_github_token") < script.rindex('echo "Summary: $summary"')


def test_shell_script_offers_a_repo_specific_env_file_when_on_the_shared_one() -> None:
    script = WF.shell_script()
    # the choice step is defined and runs before the credential check (so the retargeted file is
    # what the minted token lands in) — anchor on the flow's credential *check*.
    assert "maybe_choose_env_file" in script
    assert script.rindex("maybe_choose_env_file") < script.index("${CLAUDE_CODE_OAUTH_TOKEN:-}")
    # gated on the repo id + the repo still being on the shared secrets file (panopticon.env)
    assert "PANOPTICON_REPO_ID" in script
    assert 'panopticon.env"' in script  # the shared-file name it gates on / offers to replace
    # creates the repo-specific file, then repoints the repo record over REST via the helpers
    assert "ensure_private_env_file" in script
    assert "set_repo_env_file" in script
    # forgets the shared file's already-sourced credentials so the check reflects the repo file
    assert "unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY" in script


def test_ensure_private_env_file_creates_an_empty_private_file(tmp_path: Path) -> None:
    env_file = tmp_path / "secrets" / "acme.env"  # parent dir does not exist yet
    _sh(f"ensure_private_env_file {shlex.quote(str(env_file))}")
    assert env_file.read_text() == ""  # created empty — a token is written into it later
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600  # holds a credential → private


def test_ensure_private_env_file_leaves_an_existing_file_untouched(tmp_path: Path) -> None:
    env_file = tmp_path / "acme.env"
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-KEEP\n")
    _sh(f"ensure_private_env_file {shlex.quote(str(env_file))}")
    assert env_file.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-KEEP\n"  # not clobbered


def test_set_repo_env_file_patches_the_repo() -> None:
    # It repoints the repo record via PATCH /repos/<id> with the new env_file name (the task service
    # validates the file exists first — see ensure_private_env_file, run before this).
    script = WF.shell_script()
    assert "set_repo_env_file()" in script
    assert "--request PATCH" in script and "/repos/" in script
    assert "env_file" in script and "PANOPTICON_REPO_ID" in script


def test_is_github_url_matches_https_and_ssh_remotes() -> None:
    # Both stored forms of a github.com remote are detected; other URLs (and empty) are not.
    out = _sh(
        "for u in https://github.com/o/r.git git@github.com:o/r.git "
        "https://gitlab.com/o/r.git https://github.example.com/o/r.git ''; do "
        'if is_github_url "$u"; then echo "yes:$u"; else echo "no:$u"; fi; done'
    )
    lines = out.split()
    assert lines == [
        "yes:https://github.com/o/r.git",
        "yes:git@github.com:o/r.git",
        "no:https://gitlab.com/o/r.git",
        "no:https://github.example.com/o/r.git",
        "no:",
    ]


def test_env_file_has_var_detects_only_active_lines(tmp_path: Path) -> None:
    env_file = tmp_path / "repo.env"
    env_file.write_text("ANTHROPIC_API_KEY=key-123\n# GH_TOKEN=commented\n")
    q = shlex.quote(str(env_file))
    # a commented line doesn't count as present
    assert _sh(f"env_file_has_var GH_TOKEN {q} && echo present || echo absent").strip() == "absent"
    # an active line does
    env_file.write_text("GH_TOKEN=ghp_active\n")
    assert _sh(f"env_file_has_var GH_TOKEN {q} && echo present || echo absent").strip() == "present"
    # a missing file is absent (not an error)
    missing = shlex.quote(str(tmp_path / "nope.env"))
    assert (
        _sh(f"env_file_has_var GH_TOKEN {missing} && echo present || echo absent").strip()
        == "absent"
    )


def test_append_env_var_appends_privately(tmp_path: Path) -> None:
    # A file whose last line has no trailing newline: the helper adds a separator so the line stands
    # alone, keeps the prior content, and leaves the file private (0600).
    env_file = tmp_path / "repo.env"
    env_file.write_text("ANTHROPIC_API_KEY=key-123")  # no trailing newline
    _sh(f"append_env_var GH_TOKEN ghp_tok {shlex.quote(str(env_file))}")
    assert env_file.read_text() == "ANTHROPIC_API_KEY=key-123\nGH_TOKEN=ghp_tok\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_append_env_var_creates_a_private_file(tmp_path: Path) -> None:
    env_file = tmp_path / "secrets" / "repo.env"  # parent dir does not exist yet
    _sh(f"append_env_var GH_TOKEN ghp_tok {shlex.quote(str(env_file))}")
    assert env_file.read_text() == "GH_TOKEN=ghp_tok\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_docker_workflows_have_no_shell_script_and_default_knobs() -> None:
    from panopticon.workflows import Spike

    spike = Spike()
    assert spike.shell_script() == ""  # the base default; only shell workflows override it
    assert spike.clone_repo is False  # the base defaults; a docker task clones regardless
    assert spike.shell_workdir is None
