"""The category table's matcher rules, in isolation from the gap-analysis walk."""

from __future__ import annotations

from panopticon.profiler.categories import ASK_USER_QUESTION_TOOL, TOOL_CATEGORIES, categorize


def test_bash_pytest_is_tests() -> None:
    assert categorize("Bash", {"command": "uv run pytest tests/test_foo.py -x"}) == "tests"


def test_bash_git_and_gh_are_vcs() -> None:
    assert categorize("Bash", {"command": "git status"}) == "vcs"
    assert categorize("Bash", {"command": "gh pr create --title x"}) == "vcs"


def test_bash_deps_install_is_deps() -> None:
    assert categorize("Bash", {"command": "uv add httpx"}) == "deps"
    assert categorize("Bash", {"command": "uv sync"}) == "deps"
    assert categorize("Bash", {"command": "pip install -r requirements.txt"}) == "deps"


def test_bash_pexpect_pyte_verify_skill_are_pty_verify() -> None:
    assert categorize("Bash", {"command": "python3 -c 'import pexpect'"}) == "pty-verify"
    assert categorize("Bash", {"command": "python3 -c 'import pyte'"}) == "pty-verify"
    assert categorize("Bash", {"command": "./verify-skill.sh"}) == "pty-verify"


def test_bash_gh_does_not_match_substring_words() -> None:
    # `gh` must match as a whole word — a var named `gh_token` shouldn't trip the vcs bucket.
    assert categorize("Bash", {"command": "echo $gh_token"}) == "other-tools"


def test_bash_with_no_matching_pattern_is_other_tools() -> None:
    assert categorize("Bash", {"command": "echo hello"}) == "other-tools"


def test_bash_with_missing_command_is_other_tools() -> None:
    assert categorize("Bash", {}) == "other-tools"
    assert categorize("Bash", None) == "other-tools"


def test_read_write_edit_grep_glob_are_code_nav() -> None:
    for name in ("Read", "Write", "Edit", "Grep", "Glob"):
        assert categorize(name, {}) == "code-nav"


def test_task_and_agent_are_subagents() -> None:
    assert categorize("Task", {"description": "research"}) == "subagents"
    assert categorize("Agent", {"description": "research"}) == "subagents"


def test_mcp_prefixed_tools_are_orchestration() -> None:
    assert categorize("mcp__panopticon__set_slug", {}) == "orchestration"
    assert categorize("mcp__panopticon__advance", {}) == "orchestration"


def test_unknown_tool_falls_through_to_other_tools() -> None:
    assert categorize("SomeFutureTool", {}) == "other-tools"
    assert categorize("", {}) == "other-tools"


def test_ask_user_question_is_not_a_categorize_result() -> None:
    # AskUserQuestion is routed to operator-wait by the parser, not classified as a tool category —
    # categorize() itself would fall through to other-tools if ever called on it directly.
    assert ASK_USER_QUESTION_TOOL not in TOOL_CATEGORIES
    assert categorize(ASK_USER_QUESTION_TOOL, {}) == "other-tools"
