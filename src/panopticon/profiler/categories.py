"""The tool-time category table (single source of truth — extend it here, nowhere else).

Classifies one tool call (name + its ``tool_use`` input) into a bucket. Ordered, first-match-wins:
a ``Bash`` command is sub-classified by regex over its command string; every other tool is
classified by name. Callers needing display order (CLI/dashboard) import :data:`DISPLAY_ORDER`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: Bash-command sub-categories, checked in order against the command string. First match wins;
#: a command matching none of these falls through to ``other-tools``.
_BASH_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tests", re.compile(r"\bpytest\b")),
    ("vcs", re.compile(r"\b(git|gh)\b")),
    ("deps", re.compile(r"\b(pip3?\s+install|uv\s+(?:add|sync)|uv\s+pip\s+install)\b")),
    ("pty-verify", re.compile(r"\b(pexpect|pyte|verify[-_]skill)\b")),
)

#: Read/inspection tools that don't change repo state or wait on anything external.
_CODE_NAV_TOOLS = frozenset({"Read", "Write", "Edit", "Grep", "Glob"})

#: The subagent tool. Its tool_use→tool_result gap already spans the whole sidechain run (we never
#: walk the sidechain transcript separately, so this is one bucket, not double-counted). Claude
#: Code's built-in name is ``Task``; ``Agent`` covers harnesses (like this one) that rename it.
_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})

#: A tool call whose PreToolUse/PostToolUse hooks flip the turn to the user while it's pending
#: (see AGENTS.md's turn-flip contract) — the agent is blocked on a human answer, not doing tool
#: work. Routed to operator-wait by the parser, not classified here as a tool category.
ASK_USER_QUESTION_TOOL = "AskUserQuestion"

#: Every category a tool call can land in, in the order the CLI/dashboard display them. ``llm`` and
#: ``unattributed`` aren't tool categories (the parser computes them directly from gaps), but share
#: this ordering so rendering stays in one place.
DISPLAY_ORDER: tuple[str, ...] = (
    "llm",
    "tests",
    "pty-verify",
    "code-nav",
    "vcs",
    "deps",
    "subagents",
    "orchestration",
    "other-tools",
    "unattributed",
)

#: The tool-classified categories only (excludes ``llm``/``unattributed``) — what :func:`categorize`
#: can return.
TOOL_CATEGORIES: tuple[str, ...] = tuple(
    c for c in DISPLAY_ORDER if c not in ("llm", "unattributed")
)


def categorize(tool_name: str, tool_input: Mapping[str, Any] | None) -> str:
    """Classify one tool call into a category name (always one of :data:`TOOL_CATEGORIES`).

    ``tool_name`` is matched defensively — an unrecognized or missing name falls through to
    ``other-tools`` rather than raising, so an old-format or unfamiliar transcript is still
    handled (never crashes the profiler)."""
    if tool_name == "Bash":
        command = (tool_input or {}).get("command")
        if isinstance(command, str):
            for category, pattern in _BASH_CATEGORY_PATTERNS:
                if pattern.search(command):
                    return category
        return "other-tools"
    if tool_name in _CODE_NAV_TOOLS:
        return "code-nav"
    if tool_name in _SUBAGENT_TOOLS:
        return "subagents"
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        # The container's claude connects to the task service's MCP server and *only* it
        # (`--strict-mcp-config`), so any `mcp__` call is by construction an orchestration call.
        return "orchestration"
    return "other-tools"
