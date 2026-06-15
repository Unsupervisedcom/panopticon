"""claude turn-flip hooks: the rendered settings and the callback that POSTs `set_turn`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.container import hook
from panopticon.container.hooks import settings, write_settings


def test_settings_wire_stop_to_user_and_prompt_to_agent() -> None:
    s = settings()
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("hook user")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook agent")


def test_write_settings_writes_claude_settings(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"
    assert "Stop" in json.loads(path.read_text())["hooks"]


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
        self.calls.append((task_id, turn))
        return {}


def test_hook_flips_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient()
    assert hook.main(["user"], client=client) == 0  # type: ignore[arg-type]
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user"), ("t1", "agent")]


def test_hook_rejects_unknown_event() -> None:
    assert hook.main(["nonsense"], client=_FakeClient()) == 2  # type: ignore[arg-type]
