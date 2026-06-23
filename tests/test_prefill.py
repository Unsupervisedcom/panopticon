"""The input-box prefill poller (`sessionservice.prefill`): unit tests drive `prefill_pane` with a
fake tmux command-runner + an injected `sleep`/raw-log, pinning the emitted tmux commands and every
best-effort give-up path. No real tmux, no docker, no LLM."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from panopticon.sessionservice.prefill import BRACKETED_PASTE_ON, prefill_pane


class _Tmux:
    """Records tmux calls; `display-message` returns the configured pane id (one per call, then
    empty — so a short list simulates the session vanishing)."""

    def __init__(self, panes: Sequence[str] = ("%1",)) -> None:
        self.calls: list[list[str]] = []
        self._panes = list(panes)

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        if "display-message" in args:
            return (self._panes.pop(0) if self._panes else "") + "\n"
        return ""


def _prompt(tmp_path: Path, text: str = "build the thing") -> Path:
    path = tmp_path / "prompt.txt"
    path.write_text(text)
    return path


def test_pastes_the_prompt_once_the_input_box_is_ready(tmp_path: Path) -> None:
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(b"")  # not ready yet — no bracketed-paste-enable seen
    tmux = _Tmux(panes=["%1", "%1", "%1"])

    def sleep(_seconds: float) -> None:  # claude enables bracketed paste between polls
        raw.write_bytes(BRACKETED_PASTE_ON)

    ok = prefill_pane("sess", str(prompt), run=tmux, sleep=sleep, raw_log=str(raw), timeout=5)

    assert ok is True
    cmds = tmux.calls
    assert cmds[0] == ["tmux", "display-message", "-p", "-t", "sess", "#{pane_id}"]  # resolve pane
    assert ["tmux", "pipe-pane", "-O", "-t", "%1", f"cat >> {shlex.quote(str(raw))}"] in cmds  # tee
    assert ["tmux", "load-buffer", "-b", "panopticon-prefill-sess", str(prompt)] in cmds
    # bracketed paste (-p) into the pane, buffer deleted after (-d) — lands unsent in the box
    assert ["tmux", "paste-buffer", "-p", "-d", "-b", "panopticon-prefill-sess", "-t", "%1"] in cmds
    assert cmds[-1] == ["tmux", "pipe-pane", "-t", "%1"]  # tee stopped on cleanup


def test_gives_up_and_does_not_paste_when_the_box_never_appears(tmp_path: Path) -> None:
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(b"")
    tmux = _Tmux(panes=["%1"] * 10)
    sleeps: list[float] = []

    ok = prefill_pane("sess", str(prompt), run=tmux, sleep=sleeps.append, raw_log=str(raw), timeout=3)

    assert ok is False
    assert not any(c[:2] == ["tmux", "paste-buffer"] for c in tmux.calls)  # nothing pasted
    assert len(sleeps) == 3  # polled `timeout` times before giving up
    assert ["tmux", "pipe-pane", "-t", "%1"] in tmux.calls  # tee still stopped


def test_no_op_for_an_empty_or_whitespace_prompt(tmp_path: Path) -> None:
    tmux = _Tmux()
    assert prefill_pane("sess", str(_prompt(tmp_path, "   \n")), run=tmux) is False
    assert tmux.calls == []  # didn't even resolve the pane


def test_no_op_when_the_prompt_file_is_missing(tmp_path: Path) -> None:
    tmux = _Tmux()
    assert prefill_pane("sess", str(tmp_path / "nope.txt"), run=tmux) is False
    assert tmux.calls == []


def test_aborts_when_the_session_is_already_gone(tmp_path: Path) -> None:
    tmux = _Tmux(panes=[""])  # display-message resolves no pane
    assert prefill_pane("sess", str(_prompt(tmp_path)), run=tmux) is False
    assert not any("pipe-pane" in c for c in tmux.calls)  # never started the tee


def test_aborts_when_the_session_vanishes_mid_wait(tmp_path: Path) -> None:
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(b"")  # never becomes ready
    tmux = _Tmux(panes=["%1", ""])  # resolves once, then the session is gone

    ok = prefill_pane("sess", str(prompt), run=tmux, sleep=lambda _s: None, raw_log=str(raw), timeout=5)

    assert ok is False
    assert not any(c[:2] == ["tmux", "paste-buffer"] for c in tmux.calls)


def test_removes_the_prompt_file_after_pasting(tmp_path: Path) -> None:
    prompt, raw = _prompt(tmp_path), tmp_path / "raw.log"
    raw.write_bytes(BRACKETED_PASTE_ON)  # ready immediately
    prefill_pane("sess", str(prompt), run=_Tmux(), sleep=lambda _s: None, raw_log=str(raw), timeout=5)
    assert not prompt.exists()  # the poller owns cleanup of its throwaway prompt file
