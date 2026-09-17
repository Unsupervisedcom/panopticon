"""claude turn-flip hooks: the rendered settings and the callback that POSTs `set_turn`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from panopticon.container import hook
from panopticon.container.hooks import THEME, settings, write_settings


def test_settings_wire_stop_to_user_and_prompt_to_agent() -> None:
    s = settings()
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("hook user stop")
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook agent prompt")


def test_settings_flip_to_user_while_asking_a_question_and_back_when_answered() -> None:
    # AskUserQuestion is a mid-turn tool call (never fires Stop), so PreToolUse/PostToolUse matched
    # to it carry the turn: user while the question is pending, agent once it's answered.
    s = settings()
    pre = s["hooks"]["PreToolUse"][0]
    post = s["hooks"]["PostToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion" and post["matcher"] == "AskUserQuestion"
    assert pre["hooks"][0]["command"].endswith("hook user")  # no event arg → pure turn flip
    assert post["hooks"][0]["command"].endswith("hook agent")


def test_settings_pre_accept_bypass_permissions_mode() -> None:
    # Without this, unattended claude (--dangerously-skip-permissions) hangs on the first-run
    # "Bypass Permissions mode" acceptance prompt — the task shows "stuck starting".
    assert settings()["skipDangerousModePermissionPrompt"] is True


def test_write_settings_writes_claude_settings(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"
    assert "Stop" in json.loads(path.read_text())["hooks"]


def test_write_settings_seeds_the_starting_theme(tmp_path: Path) -> None:
    # claude has no --theme flag and defaults to a fixed dark; `auto` follows the terminal's
    # background, so an attached pane is readable on a light or dark terminal alike.
    path = write_settings(tmp_path)
    assert THEME == "auto"
    assert json.loads(path.read_text())["theme"] == "auto"


def test_write_settings_keeps_a_theme_the_container_already_chose(tmp_path: Path) -> None:
    # Seeded, not enforced: the config dir outlives a respawn, so a `/theme` run inside the
    # container must survive the next launch — while the hooks are still re-applied.
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"theme": "dark"}')

    write_settings(tmp_path)

    data = json.loads(settings_path.read_text())
    assert data["theme"] == "dark"  # the operator's choice wins
    assert "Stop" in data["hooks"]


def test_write_settings_merges_without_clobbering_existing_keys(tmp_path: Path) -> None:
    # Routed through the read-merge-write helper: any unrelated settings already on disk survive.
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"model": "opus"}')

    write_settings(tmp_path)

    data = json.loads(settings_path.read_text())
    assert data["model"] == "opus"  # preserved
    assert "Stop" in data["hooks"]  # turn-flip hooks merged in


class _FakeClient:
    def __init__(self, slug: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._slug = slug

    def set_turn(self, task_id: str, turn: str) -> dict[str, object]:
        self.calls.append((task_id, turn))
        return {}

    def get_task(self, task_id: str) -> dict[str, object]:
        return {"id": task_id, "slug": self._slug}

    def get_briefing(self, task_id: str) -> str:
        return "PHASE BRIEFING: you are in PLANNING"


def test_hook_flips_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")  # slugged → no nudge, just the turn flip
    assert hook.main(["user"], client=client, stdin=io.StringIO("")) == 0  # type: ignore[arg-type]
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user"), ("t1", "agent")]


def test_bare_flip_is_a_pure_turn_change_with_no_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The AskUserQuestion hooks pass no event arg: they only flip the turn — no briefing/nudge to
    # the agent's context (unslugged, which would otherwise nudge), no side-effects (stdin ignored).
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug=None)
    assert hook.main(["agent"], client=client) == 0  # type: ignore[arg-type]
    assert hook.main(["user"], client=client, stdin=io.StringIO("{}")) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "agent"), ("t1", "user")]  # turns flipped
    assert capsys.readouterr().out == ""  # nothing else happened


def test_hook_rejects_unknown_event() -> None:
    assert hook.main(["nonsense"], client=_FakeClient()) == 2  # type: ignore[arg-type]
    assert (
        hook.main(["user", "bogus"], client=_FakeClient()) == 2
    )  # bad event arg  # type: ignore[arg-type]
    assert (
        hook.main(["user", "prompt", "extra"], client=_FakeClient()) == 2
    )  # too many args  # type: ignore[arg-type]


def test_user_turn_briefs_the_phase_and_nudges_provision_while_unslugged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    assert hook.main(["agent", "prompt"], client=_FakeClient(slug=None)) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "PHASE BRIEFING" in out  # the current-phase briefing reaches the agent's context
    assert "provision" in out  # and, unslugged, the provisioning nudge


def test_briefing_prints_but_no_nudge_once_slugged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    hook.main(["agent", "prompt"], client=_FakeClient(slug="fix-widget"))  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert (
        "PHASE BRIEFING" in out and "provision" not in out
    )  # briefing always; nudge only unslugged


def test_stop_hook_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    # Stop hook: flips the turn but emits nothing to the agent's context.
    hook.main(["user", "stop"], client=_FakeClient(slug=None), stdin=io.StringIO(""))  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""


# -- background-task gate: don't hand the turn back while background work is still running -------


def _stop(client: _FakeClient, payload: str) -> int:
    """Run the Stop hook feeding `payload` as the hook's stdin JSON."""
    return hook.main(["user", "stop"], client=client, stdin=io.StringIO(payload))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        '{"background_tasks": [{"id": "t", "type": "shell", "status": "running"}]}',
        '{"background_tasks": [{"id": "m", "type": "monitor", "status": "running"}]}',  # Monitor tool
        '{"background_tasks": [{"id": "a", "type": "subagent", "status": "running"}]}',  # background agent
        '{"background_tasks": [{"id": "w", "type": "workflow", "status": "running"}]}',  # background workflow
        '{"background_tasks": [{"id": "t", "status": "completed"}, {"id": "u", "status": "running"}]}',
        '{"background_tasks": [{"id": "t"}]}',  # no status → treated as live (conservative)
    ],
)
def test_stop_does_not_flip_while_a_background_task_is_live(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    assert _stop(client, payload) == 0
    assert client.calls == []  # turn left on the agent — set_turn not called


@pytest.mark.parametrize(
    "payload",
    [
        "",  # empty stdin
        "   ",  # blank stdin
        "not json",  # unparseable
        "[]",  # JSON, but not an object
        "{}",  # object without the field
        '{"background_tasks": []}',  # field present, nothing running
        '{"background_tasks": [{"id": "t", "status": "completed"}]}',  # only terminal entries
        '{"background_tasks": [{"id": "t", "status": "FAILED"}]}',  # terminal, case-insensitive
        '{"background_tasks": "oops"}',  # field present but wrong type → degrade, flip
    ],
)
def test_stop_flips_to_user_when_no_live_background_task(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    assert _stop(client, payload) == 0
    assert client.calls == [("t1", "user")]  # degrades to the original turn flip


def test_background_task_does_not_suppress_the_askuserquestion_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate is the *Stop* event only. AskUserQuestion's bare `hook user` flip means the agent is
    # genuinely waiting on the user, so it must flip to user even while a background task runs.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    payload = '{"background_tasks": [{"id": "t", "status": "running"}]}'
    assert hook.main(["user"], client=client, stdin=io.StringIO(payload)) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "user")]


def test_user_prompt_submit_unaffected_by_background_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The gate is Stop-only: UserPromptSubmit (agent) always flips and still briefs, even if the
    # payload carries running background tasks.
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    client = _FakeClient(slug="fix-widget")
    payload = '{"background_tasks": [{"id": "t", "status": "running"}]}'
    assert hook.main(["agent", "prompt"], client=client, stdin=io.StringIO(payload)) == 0  # type: ignore[arg-type]
    assert client.calls == [("t1", "agent")]
    assert "PHASE BRIEFING" in capsys.readouterr().out
