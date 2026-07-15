"""Unit tests for panopticon.terminal.smoketest (the undocumented `panopticon smoketest`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from panopticon.terminal import smoketest


class FakeClient:
    """A stand-in exposing just ``list_tasks`` — the only method smoketest calls."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._tasks


def _no_sleep(_seconds: float) -> None:
    pass


def test_evaluate_succeeds_when_pane_shows_the_mint_prompt() -> None:
    client = FakeClient([{"id": "t1", "workflow": "setup-repo"}])
    lines: list[str] = []

    def run_tmux(args: Sequence[str]) -> str:
        # The pane capture carries the marker; anything else (list-sessions, …) is empty.
        if args[0] == "capture-pane":
            return f"...\nor press Enter to {smoketest.MINT_MARKER}.\n> "
        return ""

    rc = smoketest.evaluate(client, run_tmux=run_tmux, sleep=_no_sleep, out=lines.append)
    assert rc == 0
    assert any("reached the token-mint prompt" in line for line in lines)


def test_evaluate_fails_when_no_setup_repo_task_appears() -> None:
    client = FakeClient([{"id": "x", "workflow": "github-peer-reviewed"}])
    lines: list[str] = []

    def run_tmux(_args: Sequence[str]) -> str:
        return ""

    rc = smoketest.evaluate(client, timeout=3, run_tmux=run_tmux, sleep=_no_sleep, out=lines.append)
    assert rc == 1
    assert any("no setup-repo task appeared" in line for line in lines)


def test_evaluate_fails_and_dumps_diagnostics_when_prompt_never_shows() -> None:
    client = FakeClient([{"id": "t9", "workflow": "setup-repo"}])
    lines: list[str] = []
    calls: list[Sequence[str]] = []

    def run_tmux(args: Sequence[str]) -> str:
        calls.append(args)
        return "no prompt here"

    rc = smoketest.evaluate(client, timeout=3, run_tmux=run_tmux, sleep=_no_sleep, out=lines.append)
    assert rc == 1
    output = "\n".join(lines)
    assert "did not reach the token-mint prompt" in output
    # It targets the setup-repo task's `panopticon-<id>` session and dumps diagnostics on failure.
    assert any("panopticon-t9" in arg for call in calls for arg in call)
    assert "list-sessions" in output
