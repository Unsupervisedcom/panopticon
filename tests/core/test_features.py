"""Feature flags (ADR 0014 §7): the codex gate, its env parsing, and the availability helpers.

Every reader takes an explicit ``env`` mapping, so these assert the policy itself rather than
whatever the developer exported (the suite's autouse fixture clears the flag besides).
"""

from __future__ import annotations

import pytest

from panopticon.core.features import (
    CODEX_FLAG,
    agent_cli_unavailable_detail,
    available_agent_clis,
    codex_enabled,
    flag_enabled,
    require_available_agent_cli,
)


def test_codex_is_off_by_default() -> None:
    # The shipped default: no flag set → codex is not selectable (ADR 0014 §7).
    assert codex_enabled({}) is False
    assert available_agent_clis({}) == ("claude",)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_truthy_env_values_enable_the_flag(value: str) -> None:
    assert flag_enabled(value) is True
    assert codex_enabled({CODEX_FLAG: value}) is True
    assert available_agent_clis({CODEX_FLAG: value}) == ("claude", "codex")


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "maybe"])
def test_everything_else_leaves_the_flag_off(value: str | None) -> None:
    # The flag fails **closed**: an unrecognized value disables codex rather than enabling it.
    assert flag_enabled(value) is False
    assert codex_enabled({} if value is None else {CODEX_FLAG: value}) is False


def test_no_cli_named_is_always_available() -> None:
    # None = "use the repo default / the built-in default" — never something to reject.
    assert agent_cli_unavailable_detail(None, {}) is None
    require_available_agent_cli(None, {})


def test_claude_is_always_available() -> None:
    assert agent_cli_unavailable_detail("claude", {}) is None
    require_available_agent_cli("claude", {})


def test_disabled_codex_names_the_flag_that_would_enable_it() -> None:
    detail = agent_cli_unavailable_detail("codex", {})
    assert detail is not None
    assert "codex" in detail and CODEX_FLAG in detail
    with pytest.raises(ValueError, match=CODEX_FLAG):
        require_available_agent_cli("codex", {})


def test_enabled_codex_is_available() -> None:
    assert agent_cli_unavailable_detail("codex", {CODEX_FLAG: "1"}) is None
    require_available_agent_cli("codex", {CODEX_FLAG: "1"})


def test_an_unknown_cli_lists_what_is_available() -> None:
    # Not a flag problem — don't send the operator looking for one.
    detail = agent_cli_unavailable_detail("nope", {})
    assert detail is not None
    assert "unknown agent CLI 'nope'" in detail and CODEX_FLAG not in detail
    with pytest.raises(ValueError, match="unknown agent CLI 'nope'"):
        require_available_agent_cli("nope", {})
