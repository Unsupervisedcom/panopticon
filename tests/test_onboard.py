"""Tests for ``panopticon onboard`` — the Claude-backed first-run launcher."""

from __future__ import annotations

from panopticon.terminal.onboard import INITIAL_PROMPT, WIZARD_CONTEXT, run_onboard


def test_happy_path_calls_claude_with_correct_argv() -> None:
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        captured.append(argv)
        return 0

    rc = run_onboard(runner=fake_runner)

    assert rc == 0
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "claude"
    assert "--append-system-prompt" in argv
    sys_idx = argv.index("--append-system-prompt")
    assert argv[sys_idx + 1] == WIZARD_CONTEXT
    assert argv[-1] == INITIAL_PROMPT


def test_wizard_context_covers_key_sections() -> None:
    for section in ("Prerequisites", "Build", "Auth", "Start", "repo", "task", "Troubleshoot"):
        assert section.lower() in WIZARD_CONTEXT.lower(), f"WIZARD_CONTEXT missing section: {section!r}"


def test_nonzero_returncode_propagated() -> None:
    rc = run_onboard(runner=lambda _argv: 1)
    assert rc == 1


def test_custom_claude_bin() -> None:
    captured: list[list[str]] = []
    run_onboard(claude_bin="/usr/local/bin/claude", runner=lambda argv: captured.append(argv) or 0)
    assert captured[0][0] == "/usr/local/bin/claude"
