"""The gap-analysis algorithm, over a hand-built fixture transcript.

Covers every category, an ``AskUserQuestion`` span (must land in operator-wait, not a tool
category), a ``Task`` subagent call (one bucket — we never walk its sidechain), parallel tool
calls in one turn, a same-``message.id`` multi-line assistant turn (thinking → text → tool_use), a
mid-session restart (two consecutive ``user`` lines, no assistant turn between), a cross-file
session restart (between-sessions), and defensive old-format/malformed input."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from panopticon.profiler.parse import profile_transcripts


def _line(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def _user(ts: str, content: Any) -> str:
    return _line(type="user", timestamp=ts, message={"role": "user", "content": content})


def _assistant(ts: str, msg_id: str, content: list[dict[str, Any]]) -> str:
    return _line(
        type="assistant",
        timestamp=ts,
        message={"id": msg_id, "role": "assistant", "content": content},
    )


def _tool_use(tool_id: str, name: str, input_: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_}


def _tool_result(tool_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


#: One task's worth of fixture data across two session files, exercising every bucket the profiler
#: recognizes. Timestamps are hand-picked so each gap's expected classification/duration is
#: unambiguous — see the assertions below for the derivation of each number.
def _session_one() -> str:
    lines = [
        # t=0: real human prompt.
        _user("2026-03-01T00:00:00.000Z", "start the task"),
        # A turn streamed across 3 lines (thinking, text, tool_use) sharing one message id — the
        # whole span (0->10s from the prior boundary) must land in llm_s, not just the gap to the
        # first block.
        _assistant("2026-03-01T00:00:05.000Z", "m1", [{"type": "thinking", "thinking": "..."}]),
        _assistant(
            "2026-03-01T00:00:06.000Z", "m1", [{"type": "text", "text": "ok, running tests"}]
        ),
        _assistant(
            "2026-03-01T00:00:10.000Z",
            "m1",
            [_tool_use("t1", "Bash", {"command": "uv run pytest tests/test_foo.py"})],
        ),
        # tests: 12s (10 -> 22)
        _user("2026-03-01T00:00:22.000Z", [_tool_result("t1")]),
        # vcs (git, 1s), deps (uv sync, 1s), pty-verify (pexpect, 1s), code-nav (Read, 1s),
        # orchestration (mcp__, 1s) — a parallel-call turn, 5 tool_use blocks all at the same ts.
        _assistant(
            "2026-03-01T00:00:23.000Z",
            "m2",
            [
                _tool_use("t2", "Bash", {"command": "git status"}),
                _tool_use("t3", "Bash", {"command": "uv add httpx"}),
                _tool_use("t4", "Bash", {"command": "python3 -c 'import pexpect'"}),
                _tool_use("t5", "Read", {"file_path": "/workspace/foo.py"}),
                _tool_use("t6", "mcp__panopticon__set_slug", {"slug": "x"}),
            ],
        ),
        _user(
            "2026-03-01T00:00:24.000Z",
            [
                _tool_result("t2"),
                _tool_result("t3"),
                _tool_result("t4"),
                _tool_result("t5"),
                _tool_result("t6"),
            ],
        ),
        # AskUserQuestion: 90s (24 -> 114) — must be operator-wait, not other-tools.
        _assistant(
            "2026-03-01T00:00:24.500Z",
            "m3",
            [_tool_use("q1", "AskUserQuestion", {"questions": []})],
        ),
        _user("2026-03-01T00:01:54.500Z", [_tool_result("q1")]),
        # subagent (Task): 600s (95 -> 695) — one bucket; its own sidechain is never opened.
        _assistant(
            "2026-03-01T00:01:55.000Z",
            "m4",
            [_tool_use("sub1", "Task", {"description": "research the bug"})],
        ),
        _user("2026-03-01T00:11:55.000Z", [_tool_result("sub1")]),
        # unknown/future tool name -> other-tools (1s: 697 -> 698)
        _assistant("2026-03-01T00:11:57.000Z", "m5", [_tool_use("t7", "FutureTool", {})]),
        _user("2026-03-01T00:11:58.000Z", [_tool_result("t7")]),
        # The agent stops (no tool_use) — its own restart mid-session: the abandoned tool_result
        # above is immediately followed by claude's synthetic "interrupted" prompt with NO
        # assistant turn between them (2000s, 11:58 -> 44:58) — must land in operator_wait, not
        # vanish into unattributed.
        _assistant("2026-03-01T00:11:59.000Z", "m6", [{"type": "text", "text": "done for now"}]),
        _user("2026-03-01T00:45:19.000Z", "You were interrupted. Continue."),
        # trailing unmatched tool_use (transcript "cuts off") — never closed.
        _assistant(
            "2026-03-01T00:45:20.000Z", "m7", [_tool_use("orphan", "Bash", {"command": "sleep 1"})]
        ),
        # defensive garbage: never crashes the parser.
        "",
        "not json at all",
        _line(
            type="assistant",
            timestamp=None,
            message={"id": "m8", "role": "assistant", "content": []},
        ),
    ]
    return "\n".join(lines)


def _session_two() -> str:
    # A second session file — a restart between sessions (the whole file, not a mid-session gap).
    return "\n".join(
        [
            _user("2026-03-01T02:00:00.000Z", "let's continue"),
            _assistant("2026-03-01T02:00:03.000Z", "n1", [{"type": "text", "text": "ok"}]),
        ]
    )


def _profile(tmp_path: Path) -> dict[str, Any]:
    p1 = tmp_path / "session-1.jsonl"
    p1.write_text(_session_one())
    p2 = tmp_path / "session-2.jsonl"
    p2.write_text(_session_two())
    # Pass paths out of chronological order — profile_transcripts must sort them itself.
    return profile_transcripts([p2, p1])


def test_tests_category(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile["categories"]["tests"] == {"seconds": 12.0, "count": 1}


def test_vcs_deps_pty_verify_code_nav_orchestration_categories(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    for name in ("vcs", "deps", "pty-verify", "code-nav", "orchestration"):
        assert profile["categories"][name] == {"seconds": 1.0, "count": 1}, name


def test_other_tools_category(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile["categories"]["other-tools"] == {"seconds": 1.0, "count": 1}


def test_subagent_task_call_is_one_bucket(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile["categories"]["subagents"] == {"seconds": 600.0, "count": 1}
    top = profile["top_tool_calls"][0]
    assert top["category"] == "subagents"
    assert top["duration_s"] == 600.0
    assert top["first_line"] == "research the bug"


def test_ask_user_question_is_operator_wait_not_a_tool_category(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    # AskUserQuestion (90s) + the mid-session restart gap (2000s) both land in operator_wait.
    assert profile["operator_wait_s"] == 90.0 + 2000.0
    assert profile["operator_wait_count"] == 2
    # And it must never leak into other-tools (which is exactly 1 call/2s, the FutureTool call).
    assert profile["categories"]["other-tools"]["count"] == 1


def test_multiline_assistant_turn_bills_whole_span_to_llm_time(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    # The first llm gap spans the merged m1 turn (thinking @5, text @6, tool_use @10) from the
    # t=0 human prompt: 10s, not just the 5s to the turn's first block.
    assert profile["llm_count"] >= 1
    assert profile["unattributed_s"] == 0.0


def test_between_sessions_gap(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile["session_count"] == 2
    assert profile["between_sessions_count"] == 1
    # session 1 ends at 00:45:20 (the trailing orphan tool_use); session 2 starts at 02:00:00.
    assert profile["between_sessions_s"] > 3600


def test_unmatched_trailing_tool_use_is_counted_not_crashed(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile["unmatched_tool_calls"] == 1


def test_malformed_and_old_format_lines_never_crash(tmp_path: Path) -> None:
    # blank line, non-JSON line, and a null-timestamp assistant line are all present in the
    # fixture (see _session_one) — profile_transcripts must tolerate every one of them.
    profile = _profile(tmp_path)
    assert profile["total_wall_s"] > 0


def test_top_five_longest_tool_calls_sorted_descending(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    durations = [c["duration_s"] for c in profile["top_tool_calls"]]
    assert durations == sorted(durations, reverse=True)
    assert len(profile["top_tool_calls"]) <= 5


def test_accounted_time_reconciles_to_wall_time(tmp_path: Path) -> None:
    """Every second is accounted for — with one documented exception: the fixture's m2 turn fires
    5 *parallel* tool_use calls (vcs/deps/pty-verify/code-nav/orchestration) that each bill their
    own full 1s duration even though they overlap the same real wall-clock second, so summed
    category time can exceed total wall time by exactly that overlap (4 extra tool-seconds for 1
    real second here). ``unattributed_s`` is clamped at 0 rather than going negative for it."""
    profile = _profile(tmp_path)
    tool_categories_s = sum(b["seconds"] for b in profile["categories"].values())
    accounted = (
        tool_categories_s
        + profile["llm_s"]
        + profile["operator_wait_s"]
        + profile["between_sessions_s"]
    )
    assert profile["unattributed_s"] == 0.0
    assert accounted - profile["total_wall_s"] == 4.0  # the 5-parallel-calls-in-1s overlap


def test_empty_input_never_crashes() -> None:
    profile = profile_transcripts([])
    assert profile["total_wall_s"] == 0.0
    assert profile["session_count"] == 0
    assert profile["top_tool_calls"] == []


def test_missing_file_never_crashes(tmp_path: Path) -> None:
    profile = profile_transcripts([tmp_path / "does-not-exist.jsonl"])
    assert profile["total_wall_s"] == 0.0


def test_entirely_garbage_file_never_crashes(tmp_path: Path) -> None:
    p = tmp_path / "garbage.jsonl"
    p.write_text('not json\n\n{}\n{"type": "assistant"}\n')
    profile = profile_transcripts([p])
    assert profile["total_wall_s"] == 0.0
    assert profile["session_count"] == 0
