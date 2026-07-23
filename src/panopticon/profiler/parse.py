"""Pure gap-analysis over claude session transcripts: transcript file paths -> a profile dict.

No agent-side instrumentation — every timestamp already lives in the transcript. Claude's hook
events (Stop/UserPromptSubmit) aren't recorded inline, so turn boundaries are **inferred
structurally** from the transcript's own shape:

- One logical assistant turn can span several consecutive ``assistant``-type JSONL lines that
  share the same ``message.id`` (one line per completed content block — ``thinking``, then
  ``text``, then ``tool_use`` — each stamped when *that block* finished streaming). These are
  merged into one turn before any gap math, so streaming sub-line timestamps are never mistaken
  for turn boundaries.
- ``UserEvent.ts -> next AssistantTurn.end_ts`` is always **LLM time** (model latency +
  generation) — covers both a tool_result delivery and a genuine human prompt, and the turn's
  *whole* span (through any thinking/text blocks preceding a trailing tool_use), not just the gap
  to its first block.
- ``AssistantTurn -> next UserEvent`` is **tool time** when the turn carries ``tool_use`` block(s)
  (paired to their ``tool_result`` by id and classified via :mod:`panopticon.profiler.categories`),
  or **operator wait** when the turn has none (the agent stopped, handing back to the human).
  ``AskUserQuestion`` is a tool_use, but its span is a blocked-on-human wait (the PreToolUse/
  PostToolUse hooks flip the turn to the user while it's pending), so it's routed to operator
  wait, not a tool category.
- Multiple session files (restarts/phases) are concatenated by timestamp; the gap from one
  session's last record to the next session's first is **between-sessions** wait, reported
  separately. A restart *within* one session file — two consecutive ``user``-type lines with no
  assistant turn between them, e.g. claude's "You were interrupted. Continue." synthetic prompt
  landing right after an abandoned tool_result — is billed to **operator wait** too, for the same
  reason: it's neither model generation nor tool execution.
- Anything unparseable — malformed lines, missing/unparseable timestamps, a ``tool_use`` with no
  matching ``tool_result`` (a transcript cut off mid-call) — never crashes the parser; durations we
  can't attribute show up in ``unattributed_s``/``unmatched_tool_calls`` rather than being dropped
  silently or guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from panopticon.profiler.categories import ASK_USER_QUESTION_TOOL, TOOL_CATEGORIES, categorize


@dataclass
class _ToolCall:
    tool_use_id: str
    name: str
    input: dict[str, Any]
    call_ts: float


@dataclass
class _AssistantTurn:
    start_ts: float
    end_ts: float
    tool_calls: list[_ToolCall] = field(default_factory=list)


@dataclass
class _UserEvent:
    ts: float
    tool_result_ts: dict[str, float]  # tool_use_id -> the ts this result arrived
    is_real_prompt: bool  # no tool_result blocks at all -> a genuine human message


_Event = _AssistantTurn | _UserEvent


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Every JSON-object line in ``path``; blank/non-JSON/non-object lines are skipped, never
    raised on — a missing file yields no records rather than an error."""
    try:
        text = path.read_text()
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _session_events(records: list[dict[str, Any]]) -> list[_Event]:
    """Merge same-``message.id`` assistant lines into turns and pair each with the user events
    (real prompts and tool_result deliveries) around them, in chronological (file) order."""
    events: list[_Event] = []
    pending_turn: _AssistantTurn | None = None
    pending_msg_id: object = None

    def flush_turn() -> None:
        nonlocal pending_turn, pending_msg_id
        if pending_turn is not None:
            events.append(pending_turn)
        pending_turn = None
        pending_msg_id = None

    for rec in records:
        rtype = rec.get("type")
        if rtype == "assistant":
            msg = rec.get("message")
            ts = _parse_ts(rec.get("timestamp"))
            if not isinstance(msg, dict) or ts is None:
                continue
            msg_id = msg.get("id")
            tool_calls = []
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_id, name = block.get("id"), block.get("name")
                    if isinstance(tool_id, str) and isinstance(name, str):
                        input_ = block.get("input")
                        tool_calls.append(
                            _ToolCall(tool_id, name, input_ if isinstance(input_, dict) else {}, ts)
                        )
            if pending_turn is not None and msg_id is not None and msg_id == pending_msg_id:
                pending_turn.end_ts = ts
                pending_turn.tool_calls.extend(tool_calls)
            else:
                flush_turn()
                pending_turn = _AssistantTurn(start_ts=ts, end_ts=ts, tool_calls=tool_calls)
                pending_msg_id = msg_id
        elif rtype == "user":
            flush_turn()
            msg = rec.get("message")
            ts = _parse_ts(rec.get("timestamp"))
            if not isinstance(msg, dict) or ts is None:
                continue
            content = msg.get("content")
            tool_result_ts: dict[str, float] = {}
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if isinstance(tool_use_id, str):
                            tool_result_ts[tool_use_id] = ts
            events.append(
                _UserEvent(ts=ts, tool_result_ts=tool_result_ts, is_real_prompt=not tool_result_ts)
            )
        else:
            continue  # structural noise: mode, permission-mode, file-history-snapshot, attachment, …
    flush_turn()
    return events


def _event_ts(event: _Event, *, start: bool) -> float:
    if isinstance(event, _AssistantTurn):
        return event.start_ts if start else event.end_ts
    return event.ts


class _Buckets:
    """Running totals accumulated while walking one task's concatenated sessions."""

    def __init__(self) -> None:
        self.llm_s = 0.0
        self.llm_count = 0
        self.operator_wait_s = 0.0
        self.operator_wait_count = 0
        self.between_sessions_s = 0.0
        self.between_sessions_count = 0
        self.unmatched_tool_calls = 0
        self.categories: dict[str, dict[str, float]] = {
            name: {"seconds": 0.0, "count": 0} for name in TOOL_CATEGORIES
        }
        self.top_tool_calls: list[dict[str, Any]] = []

    def record_tool_call(self, call: _ToolCall, duration: float) -> None:
        duration = max(0.0, duration)
        if call.name == ASK_USER_QUESTION_TOOL:
            self.operator_wait_s += duration
            self.operator_wait_count += 1
            return
        category = categorize(call.name, call.input)
        bucket = self.categories[category]
        bucket["seconds"] += duration
        bucket["count"] += 1
        self.top_tool_calls.append(
            {
                "category": category,
                "tool_name": call.name,
                "first_line": _first_line(call.name, call.input),
                "duration_s": duration,
            }
        )


def _first_line(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip().splitlines()[0]
    for key in ("file_path", "description", "prompt", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0]
    return name


def _walk_session(events: list[_Event], buckets: _Buckets) -> None:
    pending_tool_calls: dict[str, _ToolCall] = {}
    operator_wait_since: float | None = None
    llm_boundary_ts: float | None = None

    for ev in events:
        if isinstance(ev, _AssistantTurn):
            if llm_boundary_ts is not None:
                # Bill the *whole* turn's generation time as LLM time — including any thinking/text
                # blocks that streamed before a trailing tool_use — not just the gap to its first
                # block, which would otherwise leak that time into unattributed.
                gap = ev.end_ts - llm_boundary_ts
                if gap > 0:
                    buckets.llm_s += gap
                    buckets.llm_count += 1
            llm_boundary_ts = None
            operator_wait_since = None
            if ev.tool_calls:
                for call in ev.tool_calls:
                    pending_tool_calls[call.tool_use_id] = call
            else:
                operator_wait_since = ev.end_ts  # the agent stopped; waiting on the human next
        else:  # _UserEvent
            if llm_boundary_ts is not None:
                # Back-to-back `user`-type lines with no assistant turn between them — e.g. claude
                # injecting its "You were interrupted. Continue." synthetic prompt right after an
                # abandoned tool_result on a container respawn mid-turn. That gap is neither model
                # generation nor tool execution; bill it to operator/system wait rather than let it
                # silently vanish into unattributed.
                gap = ev.ts - llm_boundary_ts
                if gap > 0:
                    buckets.operator_wait_s += gap
                    buckets.operator_wait_count += 1
                llm_boundary_ts = None
            if ev.tool_result_ts:
                for tool_use_id, result_ts in ev.tool_result_ts.items():
                    if tool_use_id not in pending_tool_calls:
                        continue  # a tool_result with no open call — ignore defensively
                    call = pending_tool_calls.pop(tool_use_id)
                    buckets.record_tool_call(call, result_ts - call.call_ts)
                llm_boundary_ts = ev.ts
            elif operator_wait_since is not None:
                gap = ev.ts - operator_wait_since
                if gap > 0:
                    buckets.operator_wait_s += gap
                    buckets.operator_wait_count += 1
                llm_boundary_ts = ev.ts
            else:
                llm_boundary_ts = ev.ts
            operator_wait_since = None
    buckets.unmatched_tool_calls += len(pending_tool_calls)


def _empty_profile() -> dict[str, Any]:
    return {
        "total_wall_s": 0.0,
        "operator_wait_s": 0.0,
        "operator_wait_count": 0,
        "between_sessions_s": 0.0,
        "between_sessions_count": 0,
        "agent_active_s": 0.0,
        "llm_s": 0.0,
        "llm_count": 0,
        "categories": {name: {"seconds": 0.0, "count": 0} for name in TOOL_CATEGORIES},
        "unattributed_s": 0.0,
        "unmatched_tool_calls": 0,
        "session_count": 0,
        "top_tool_calls": [],
    }


def profile_transcripts(paths: list[Path] | tuple[Path, ...]) -> dict[str, Any]:
    """Gap-analyze one task's (possibly many) session transcripts into a time-profile dict.

    ``paths`` are the task's session JSONL files in any order — they're sorted by each session's
    own first-record timestamp before concatenation, so restarts/phases line up correctly
    regardless of filename or argument order. Pure aside from reading these files: no network, no
    LLM, no clock reads, so it's fully deterministic and unit-testable via ``tmp_path`` fixtures.
    """
    sessions: list[tuple[float, float, list[_Event]]] = []
    for path in paths:
        events = _session_events(_read_records(Path(path)))
        if not events:
            continue
        sessions.append(
            (_event_ts(events[0], start=True), _event_ts(events[-1], start=False), events)
        )

    if not sessions:
        return _empty_profile()

    sessions.sort(key=lambda s: s[0])
    buckets = _Buckets()
    for i, (start_ts, _end_ts, events) in enumerate(sessions):
        if i > 0:
            gap = start_ts - sessions[i - 1][1]
            if gap > 0:
                buckets.between_sessions_s += gap
                buckets.between_sessions_count += 1
        _walk_session(events, buckets)

    total_wall_s = sessions[-1][1] - sessions[0][0]
    tool_categories_s = sum(b["seconds"] for b in buckets.categories.values())
    agent_active_s = tool_categories_s + buckets.llm_s
    accounted_s = agent_active_s + buckets.operator_wait_s + buckets.between_sessions_s
    # Parallel tool calls (one turn, several tool_use blocks) each bill their own full duration
    # even though they overlap in wall time, so accounted_s can slightly *exceed* total_wall_s —
    # clamp rather than surface a confusing negative "unattributed".
    unattributed_s = max(0.0, total_wall_s - accounted_s)

    top_tool_calls = sorted(buckets.top_tool_calls, key=lambda c: c["duration_s"], reverse=True)[:5]

    return {
        "total_wall_s": total_wall_s,
        "operator_wait_s": buckets.operator_wait_s,
        "operator_wait_count": buckets.operator_wait_count,
        "between_sessions_s": buckets.between_sessions_s,
        "between_sessions_count": buckets.between_sessions_count,
        "agent_active_s": agent_active_s,
        "llm_s": buckets.llm_s,
        "llm_count": buckets.llm_count,
        "categories": buckets.categories,
        "unattributed_s": unattributed_s,
        "unmatched_tool_calls": buckets.unmatched_tool_calls,
        "session_count": len(sessions),
        "top_tool_calls": top_tool_calls,
    }
