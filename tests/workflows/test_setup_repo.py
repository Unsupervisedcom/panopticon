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

# The sourceable helpers (extract_oauth_token / store_env_token / repo_source_label / …) the
# functional tests exercise in a real `sh`, no LLM — the token is a literal fixture, `claude`/`gh`/
# `script` are never invoked.
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
    # branches on an already-configured credential, checked against the **env-file** (not the sourced
    # env) so a host-only token isn't mis-reported as configured
    assert "CLAUDE_CODE_OAUTH_TOKEN" in script and "ANTHROPIC_API_KEY" in script
    assert "env_file_has_var CLAUDE_CODE_OAUTH_TOKEN" in script
    assert "$PANOPTICON_ENV_FILE" in script or "PANOPTICON_ENV_FILE" in script  # names the env-file
    # detects/falls back to the tmux detach binding to get back to the dashboard
    assert "detach-client" in script and "show-options -gv prefix" in script


def test_shell_script_opens_with_the_credentials_goal_intro() -> None:
    script = WF.shell_script()
    # begins by explaining what happens and that the operator stays in control: task containers use
    # per-repo credentials (not the operator's own session), and they can opt out.
    assert "per-repo credentials" in script
    assert "not your" in script and "personal session" in script
    assert "set up your own secrets by editing" in script
    # the intro comes before the dashboard hint and any prompts
    assert script.index("per-repo credentials") < script.index('echo "$dashboard_hint"')


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
    # extracts the minted token and stores it via the shared, var-parameterized helper (the DRY
    # primitive both the Claude token and GH_TOKEN write through)
    assert "extract_oauth_token" in script
    assert "store_env_token CLAUDE_CODE_OAUTH_TOKEN" in script and "PANOPTICON_ENV_FILE" in script
    # the helper comments out an existing active line and drops a placeholder comment stub
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


def test_shell_script_summarizes_the_repo_and_setup_up_front() -> None:
    script = WF.shell_script()
    # opens with two bulleted lists: what we know about the repo (name + source), and what setting it
    # up entails, driven by the repo env vars the shell runner injects and the source classification.
    assert "This repo:" in script and "To set up:" in script
    assert "PANOPTICON_REPO_NAME" in script and "PANOPTICON_GIT_URL" in script
    assert "repo_source_label" in script
    assert "Claude credential" in script and "GH_TOKEN" in script


def test_shell_script_sets_up_the_github_token_for_github_repos() -> None:
    script = WF.shell_script()
    # gated on the repo being a GitHub remote (local checkouts skip the whole GH step)
    assert "is_github_url" in script and "PANOPTICON_GIT_URL" in script
    # adopts a GH_TOKEN from the environment or lets the operator paste one — it does not mint one
    assert "GH_TOKEN" in script
    assert "gh auth login" not in script and "gh auth token" not in script
    # writes it via the shared store_token → store_env_token (existing GH_TOKEN commented out +
    # replaced), and only offers to adopt one that isn't already the env-file's own
    assert "store_token GH_TOKEN" in script
    assert "env_file_has_var GH_TOKEN" in script
    # the GH step runs after the Claude credential step but before the final summary
    assert script.rindex("setup_gh_token") > script.index("claude_configured")
    assert script.rindex("setup_gh_token") < script.rindex('echo "Summary:"')


def test_shell_script_offers_adopt_paste_and_default_no_consent() -> None:
    script = WF.shell_script()
    # each credential can be adopted from the operator's env or pasted inline (fast path for an
    # already-authenticated operator — no cancel-and-restart)
    assert "Paste a Claude token" in script and "Paste a GitHub token" in script
    # adoption confirms *which* token via a masked tail, and every prompt is default-No (no [Y/n])
    assert "mask_last4" in script
    assert "[Y/n]" not in script
    # the Claude token can be adopted from a host-env var too (symmetric with GH)
    assert "setup_claude_token" in script


def test_shell_script_closing_summary_is_bulleted() -> None:
    script = WF.shell_script()
    # each step records its outcome as a bullet; the closing summary prints them
    assert "add_summary" in script
    assert 'summary="  • ' in script  # bullet-prefixed accumulation


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


def test_store_env_token_is_generic_over_the_var_name(tmp_path: Path) -> None:
    # The shared primitive works for any var (here GH_TOKEN), mirroring the Claude-token behaviour:
    # comment out the active line, drop the placeholder stub, append the new one, leave others alone.
    env_file = tmp_path / "repo.env"
    env_file.write_text(
        "GH_TOKEN=ghp_OLD\n"
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-KEEP\n"
        "# GH_TOKEN =\n"  # a placeholder stub to be removed
        "# a note we keep\n"
    )
    _sh(f"store_env_token GH_TOKEN ghp_NEW {shlex.quote(str(env_file))}")
    lines = env_file.read_text().splitlines()

    assert "# GH_TOKEN=ghp_OLD" in lines  # previous active token preserved but commented out
    assert "# GH_TOKEN =" not in lines  # placeholder stub gone
    # an unrelated var (the Claude token) and comments are untouched
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-KEEP" in lines
    assert "# a note we keep" in lines
    # exactly one *active* GH_TOKEN line, and it's the new one
    assert [ln for ln in lines if ln.startswith("GH_TOKEN=")] == ["GH_TOKEN=ghp_NEW"]
    # holds a live credential — kept private (0600)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_store_env_token_separates_the_new_line_without_a_trailing_newline(tmp_path: Path) -> None:
    # A file whose last line lacks a trailing newline still gets the appended line on its own line.
    env_file = tmp_path / "secrets" / "repo.env"  # parent dir does not exist yet
    env_file.parent.mkdir()
    env_file.write_text("ANTHROPIC_API_KEY=key-123")  # no trailing newline
    _sh(f"store_env_token GH_TOKEN ghp_tok {shlex.quote(str(env_file))}")
    assert env_file.read_text() == "ANTHROPIC_API_KEY=key-123\nGH_TOKEN=ghp_tok\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_repo_source_label_classifies_local_github_and_other() -> None:
    # Drives the opening summary's "Source:" line and the GH_TOKEN gating.
    assert _sh("repo_source_label https://github.com/acme/widget.git").strip() == "GitHub remote"
    assert _sh("repo_source_label git@github.com:acme/widget.git").strip() == "GitHub remote"
    assert _sh("repo_source_label /home/me/src/widget").strip() == "local checkout"
    assert _sh("repo_source_label file:///srv/widget").strip() == "local checkout"
    assert _sh("repo_source_label https://gitlab.com/x/y.git").strip() == "remote"
    assert _sh("repo_source_label ''").strip() == "unknown"


def test_mask_last4_reveals_only_the_tail() -> None:
    # Consent prompts show which token without exposing it: the last 4 chars, or nothing when short.
    assert _sh("mask_last4 sk-ant-oat01-abcdWXYZ").strip() == "...WXYZ"
    assert _sh("mask_last4 ghp_1234").strip() == "...1234"  # 8 chars → last 4
    assert _sh("mask_last4 abcd").strip() == "..."  # exactly 4 → nothing safe to reveal
    assert _sh("mask_last4 ab").strip() == "..."
    assert _sh("mask_last4 ''").strip() == "..."


def test_docker_workflows_have_no_shell_script_and_default_knobs() -> None:
    from panopticon.workflows import Spike

    spike = Spike()
    assert spike.shell_script() == ""  # the base default; only shell workflows override it
    assert spike.clone_repo is False  # the base defaults; a docker task clones regardless
    assert spike.shell_workdir is None
