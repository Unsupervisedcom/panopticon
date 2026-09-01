"""The container-side tarot gate is now an allow-everything shim.

Enforcement moved host-side (`tests/taskservice/test_tarot_gate.py` is the real spec). What
matters here is only that the module a stale `.claude/settings.json` still points at behaves like
a hook that permits the tool call: no stdout (claude reads a decision from stdout, so anything
printed would be one), exit 0, and no dependence on the task service or on `tarot` existing.
"""

from __future__ import annotations

import io
import json

from panopticon.container import tarot_gate


def test_allows_with_no_output(capsys) -> None:
    payload = json.dumps(
        {
            "tool_name": "mcp__panopticon__apply_operation",
            "tool_input": {"operation": "advance", "task_id": "t1"},
        }
    )
    assert tarot_gate.main(stdin=io.StringIO(payload)) == 0
    assert capsys.readouterr().out == ""


def test_allows_on_empty_or_garbage_stdin(capsys) -> None:
    for raw in ("", "   ", "not json at all", "[1, 2, 3]"):
        assert tarot_gate.main(stdin=io.StringIO(raw)) == 0
    assert capsys.readouterr().out == ""


def test_allows_when_stdin_is_closed(capsys) -> None:
    """A hook whose stdin is gone must still permit the call, not raise."""
    closed = io.StringIO("{}")
    closed.close()
    assert tarot_gate.main(stdin=closed) == 0
    assert capsys.readouterr().out == ""


def test_needs_no_service_or_tarot(monkeypatch, capsys) -> None:
    """No REST call, no subprocess — the shim must work in a container with neither available."""

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("the shim must not shell out or call the task service")

    monkeypatch.setattr("subprocess.run", explode)
    monkeypatch.delenv("PANOPTICON_SERVICE_URL", raising=False)
    monkeypatch.delenv("PANOPTICON_TASK_ID", raising=False)
    assert tarot_gate.main(stdin=io.StringIO('{"tool_input": {"operation": "advance"}}')) == 0
    assert capsys.readouterr().out == ""
