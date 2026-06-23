"""Pre-fill a freshly spawned task container's Claude Code input box (mirrors cloude-cade's
``bin/cloude-prefill-prompt``).

The runner makes a host tmux session whose pane runs ``docker exec -it … python -m
panopticon.container.agent`` — the launcher bootstraps the CLI then ``exec``s ``claude`` — so the
pane's TTY *is* claude's stdin/stdout. This poller watches that pane's **raw** output for the
bracketed-paste-enable escape (``ESC[?2004h``), emitted once when claude's TUI initializes the very
input mode the paste relies on, then bracketed-pastes the task description into the input box **left
unsent** — so the user attaches and presses Enter to begin (or edits the prompt first).

Why the escape, not on-screen wording: it's independent of any UI text, so a claude restyle can't
break it; and nothing interactive runs ahead of claude on the pane (the launcher's bootstrap is
plain non-interactive Python), so the first ``ESC[?2004h`` on the stream is unambiguously claude's.

Why paste, not type: a description may span multiple lines; ``tmux paste-buffer -p`` uses bracketed
paste, so the whole block lands in the box without a newline being treated as submit.

The runner launches this **detached** so ``spawn`` never blocks. Best-effort throughout: on an empty
prompt, an opt-out, a timeout, a vanished session, or any tmux error it gives up and leaves the box
empty — it never fails the spawn. LLM-free.

``tmux`` flags are single-letter because tmux has no long forms (CLAUDE.md's long-options rule
exempts it).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

#: Seconds to wait for claude's input box before giving up (image build + container start + claude
#: init is variable, so we poll rather than guess). Overridable via ``PANOPTICON_PREFILL_TIMEOUT``.
DEFAULT_TIMEOUT = 300.0

#: ``ESC[?2004h`` — DEC private mode 2004 set, i.e. bracketed paste enabled. The readiness signal.
BRACKETED_PASTE_ON = b"\x1b[?2004h"


class CommandRunner(Protocol):
    """Runs a tmux command and returns its stdout (same shape as the runner's command executor)."""

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str: ...


def _tmux(prefix: Sequence[str], *args: str) -> list[str]:
    return [*prefix, *args]


def _pane_id(session: str, *, prefix: Sequence[str], run: CommandRunner) -> str:
    """The session's active pane id (``%N``) — needed because a session name is a valid
    target-session but not a valid target-pane for ``pipe-pane``/``paste-buffer``. ``""`` if the
    session is gone (or any error), which the caller treats as "abort, leave the box empty"."""
    try:
        return run(_tmux(prefix, "display-message", "-p", "-t", session, "#{pane_id}"), check=False).strip()
    except OSError:
        return ""


def prefill_pane(
    session: str,
    prompt_file: str,
    *,
    run: CommandRunner,
    prefix: Sequence[str] = ("tmux",),
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = DEFAULT_TIMEOUT,
    raw_log: str | None = None,
    buffer: str | None = None,
) -> bool:
    """Poll ``session``'s pane until claude's input box is ready, then bracketed-paste
    ``prompt_file`` into it, unsent. Returns ``True`` if it pasted, ``False`` on any give-up path
    (empty prompt, vanished session, timeout, tmux error). Never raises — best-effort by design."""
    prompt = Path(prompt_file)
    try:
        if not prompt.is_file() or not prompt.read_text().strip():
            return False  # nothing to prefill (matches cloude-cade's empty-prompt-file skip)

        pane = _pane_id(session, prefix=prefix, run=run)
        if not pane:
            return False  # session not found — abort

        raw = raw_log or tempfile.mkstemp(prefix="panopticon-prefill-raw-")[1]
        buf = buffer or f"panopticon-prefill-{session}"
        created_raw = raw_log is None
        try:
            # Tee the pane's raw output (escape sequences and all) so we can watch for readiness; tmux
            # backgrounds the `cat`, so this returns at once. The poll below is what gates the paste.
            run(_tmux(prefix, "pipe-pane", "-O", "-t", pane, f"cat >> {shlex.quote(raw)}"), check=False)
            ready = False
            for _ in range(int(timeout)):
                if not _pane_id(session, prefix=prefix, run=run):
                    return False  # session vanished mid-wait
                if BRACKETED_PASTE_ON in Path(raw).read_bytes():
                    ready = True
                    break
                sleep(1.0)
            if not ready:
                return False  # timed out — leave the box empty
            sleep(1.0)  # let the frame settle before pasting
            run(_tmux(prefix, "load-buffer", "-b", buf, str(prompt)), check=False)
            run(_tmux(prefix, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane), check=False)
            return True
        finally:
            run(_tmux(prefix, "pipe-pane", "-t", pane), check=False)  # stop the tee
            if created_raw:
                _unlink(raw)
    except OSError:
        return False
    finally:
        _unlink(prompt_file)  # the poller owns its throwaway prompt file — clean it up on every path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_tmux(args: Sequence[str], *, check: bool = True) -> str:  # pragma: no cover - real tmux
    """Tolerant command executor for the detached CLI: capture stdout, never raise on a tmux
    error (best-effort), so a missing pane or a tmux hiccup just leaves the box empty."""
    try:
        return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - detached real-tmux entry
    """``python -m panopticon.sessionservice.prefill <session> <prompt-file>`` — what the runner
    launches detached. Reads the env knobs and drives :func:`prefill_pane` against the real tmux."""
    parser = argparse.ArgumentParser(description="Pre-fill a task container's Claude input box.")
    parser.add_argument("session")
    parser.add_argument("prompt_file")
    parser.add_argument("--socket", default=None, help="tmux server socket (-L)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if os.environ.get("PANOPTICON_NO_PREFILL"):
        return 0  # opt out
    try:
        timeout = float(os.environ.get("PANOPTICON_PREFILL_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    prefix = ["tmux", *(["-L", args.socket] if args.socket else [])]
    prefill_pane(args.session, args.prompt_file, run=_run_tmux, prefix=prefix, timeout=timeout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
