"""Golden-output tests for the profile renderers, and unit tests for `run_profile_command`'s
wiring (fake client + injected session-path lookup — no real docker)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panopticon.terminal.task_profile import (
    aggregate_repo_profiles,
    format_all_tasks_report,
    format_task_profile,
    format_time_summary,
    run_profile_command,
)


def _profile(**overrides: Any) -> dict[str, Any]:
    """A hand-built profile dict with clean round numbers, so the golden strings below are easy
    to hand-verify and stay decoupled from any change to the parser's own arithmetic."""
    base: dict[str, Any] = {
        "total_wall_s": 52320.0,  # 14h 32m
        "operator_wait_s": 33000.0,  # 9h 10m
        "operator_wait_count": 4,
        "between_sessions_s": 3900.0,  # 1h 05m
        "between_sessions_count": 2,
        "agent_active_s": 15420.0,  # 4h 17m
        "llm_s": 6720.0,  # 1h 52m
        "llm_count": 112,
        "categories": {
            "tests": {"seconds": 2880.0, "count": 23},  # 48m
            "pty-verify": {"seconds": 540.0, "count": 3},  # 9m
            "code-nav": {"seconds": 1860.0, "count": 87},  # 31m
            "vcs": {"seconds": 1320.0, "count": 19},  # 22m
            "deps": {"seconds": 0.0, "count": 0},
            "subagents": {"seconds": 2460.0, "count": 4},  # 41m
            "orchestration": {"seconds": 360.0, "count": 14},  # 6m
            "other-tools": {"seconds": 480.0, "count": 11},  # 8m
        },
        "unattributed_s": 0.0,
        "unmatched_tool_calls": 0,
        "session_count": 3,
        "top_tool_calls": [
            {
                "category": "tests",
                "tool_name": "Bash",
                "first_line": "uv run pytest tests/ -x",
                "duration_s": 723.0,
            },
            {
                "category": "subagents",
                "tool_name": "Task",
                "first_line": "research the schema",
                "duration_s": 580.0,
            },
        ],
    }
    base.update(overrides)
    return base


def test_format_task_profile_golden_output() -> None:
    task = {"id": "8e3e412d431e4c99b8a27a321eb8a7a2", "slug": "profiler-slice"}
    out = format_task_profile(task, _profile(), repo_name="panopticon")
    assert out == (
        "Task 8e3e412d431e4c99b8a27a321eb8a7a2 (profiler-slice) — panopticon\n"
        "  total wall:           14h 32m\n"
        "  operator wait:         9h 10m  (63%)\n"
        "  between sessions:      1h 05m  (7%)   [2 gaps]\n"
        "  agent active:          4h 17m  (29%)\n"
        "    llm               1h 52m  ( 44%)  112 calls\n"
        "    tests            48m 00s  ( 19%)  23 calls\n"
        "    pty-verify        9m 00s  (  4%)  3 calls\n"
        "    code-nav         31m 00s  ( 12%)  87 calls\n"
        "    vcs              22m 00s  (  9%)  19 calls\n"
        "    deps                  0s  (  0%)  0 calls\n"
        "    subagents        41m 00s  ( 16%)  4 calls\n"
        "    orchestration     6m 00s  (  2%)  14 calls\n"
        "    other-tools       8m 00s  (  3%)  11 calls\n"
        "\n"
        "  top 5 longest tool calls:\n"
        "    1. tests          12m 03s  uv run pytest tests/ -x\n"
        "    2. subagents       9m 40s  research the schema"
    )


def test_format_task_profile_shows_unattributed_only_when_nonzero() -> None:
    task = {"id": "t1", "slug": None}
    assert "unattributed:" not in format_task_profile(task, _profile(), repo_name=None)
    with_unattributed = format_task_profile(task, _profile(unattributed_s=60.0), repo_name=None)
    assert "unattributed:          1m 00s  (0%)" in with_unattributed


def test_format_task_profile_notes_unmatched_tool_calls() -> None:
    task = {"id": "t1", "slug": None}
    out = format_task_profile(task, _profile(unmatched_tool_calls=1), repo_name=None)
    assert "(1 tool call never got a result — transcript cut off)" in out


def test_format_task_profile_falls_back_to_id_with_no_slug() -> None:
    task = {"id": "t1", "slug": None}
    out = format_task_profile(task, _profile(), repo_name=None)
    assert out.startswith("Task t1 (-)")


def test_format_time_summary_golden_output() -> None:
    assert format_time_summary(_profile()) == (
        "agent 4.3h: llm 44% tests 19% tools 38% | waited on user 9.2h (+1.1h between sessions)"
    )


def test_format_time_summary_omits_between_sessions_when_zero() -> None:
    summary = format_time_summary(_profile(between_sessions_s=0.0))
    assert "between sessions" not in summary
    assert summary.endswith("waited on user 9.2h")


def test_aggregate_repo_profiles_sums_and_medians() -> None:
    agg = aggregate_repo_profiles(
        [_profile(), _profile(total_wall_s=10000.0, operator_wait_s=5000.0, agent_active_s=5000.0)]
    )
    assert agg["task_count"] == 2
    assert agg["totals"]["total_wall_s"] == 62320.0
    assert agg["medians"]["total_wall_s"] == (52320.0 + 10000.0) / 2


def test_aggregate_repo_profiles_of_empty_list() -> None:
    assert aggregate_repo_profiles([]) == {"task_count": 0}


def test_format_all_tasks_report_golden_output() -> None:
    agg = aggregate_repo_profiles([_profile(), _profile()])
    out = format_all_tasks_report({"panopticon": agg}, skipped={"panopticon": 3})
    assert out == (
        "repo panopticon (2 tasks profiled, 3 skipped: no transcripts found)\n"
        "  median wall:              14h 32m\n"
        "  median operator wait:      9h 10m  (63%)\n"
        "  median agent active:       4h 17m  (29%)\n"
        "\n"
        "  agent-active time by category (of 8h 34m total across 2 tasks):\n"
        "    llm               3h 44m  ( 44%)  224 calls\n"
        "    tests             1h 36m  ( 19%)  46 calls\n"
        "    pty-verify       18m 00s  (  4%)  6 calls\n"
        "    code-nav          1h 02m  ( 12%)  174 calls\n"
        "    vcs              44m 00s  (  9%)  38 calls\n"
        "    deps                  0s  (  0%)  0 calls\n"
        "    subagents         1h 22m  ( 16%)  8 calls\n"
        "    orchestration    12m 00s  (  2%)  28 calls\n"
        "    other-tools      16m 00s  (  3%)  22 calls"
    )


def test_format_all_tasks_report_skips_repos_with_no_tasks() -> None:
    out = format_all_tasks_report({"empty-repo": {"task_count": 0}})
    assert out == ""


# -- run_profile_command: wiring, no real docker -------------------------------------


class _FakeClient:
    def __init__(self, tasks: list[dict[str, Any]], repos: list[dict[str, Any]]) -> None:
        self._tasks = tasks
        self._repos = repos

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._tasks

    def list_repos(self) -> list[dict[str, Any]]:
        return self._repos


def test_run_profile_command_resolves_by_slug_and_prints(tmp_path: Path, capsys: Any) -> None:
    client = _FakeClient(
        tasks=[{"id": "t1", "slug": "my-task", "repo_id": "r1"}],
        repos=[{"id": "r1", "name": "panopticon"}],
    )
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        '{"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "message": {"role": "user", "content": "hi"}}\n'
    )

    rc = run_profile_command(
        client, task_ref="my-task", all_tasks=False, session_paths=lambda task_id: [transcript]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Task t1 (my-task) — panopticon" in out


def test_run_profile_command_unknown_task_id(capsys: Any) -> None:
    client = _FakeClient(tasks=[], repos=[])
    rc = run_profile_command(client, task_ref="nope", all_tasks=False, session_paths=lambda t: [])
    err = capsys.readouterr().err
    assert rc == 1
    assert "No such task: nope" in err


def test_run_profile_command_no_transcripts(capsys: Any) -> None:
    client = _FakeClient(tasks=[{"id": "t1", "slug": "s", "repo_id": "r1"}], repos=[])
    rc = run_profile_command(client, task_ref="s", all_tasks=False, session_paths=lambda t: [])
    err = capsys.readouterr().err
    assert rc == 1
    assert "No session transcripts found" in err


def test_run_profile_command_no_task_ref_and_not_all_tasks(capsys: Any) -> None:
    client = _FakeClient(tasks=[], repos=[])
    rc = run_profile_command(client, task_ref=None, all_tasks=False, session_paths=lambda t: [])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_run_profile_command_all_tasks_aggregates_per_repo(tmp_path: Path, capsys: Any) -> None:
    client = _FakeClient(
        tasks=[
            {"id": "t1", "slug": "a", "repo_id": "r1"},
            {"id": "t2", "slug": "b", "repo_id": "r1"},
            {"id": "t3", "slug": "c", "repo_id": "r2"},  # no transcripts -> skipped
        ],
        repos=[{"id": "r1", "name": "panopticon"}, {"id": "r2", "name": "tarot"}],
    )
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        '{"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "message": {"role": "user", "content": "hi"}}\n'
    )

    def session_paths(task_id: str) -> list[Path]:
        return [transcript] if task_id in ("t1", "t2") else []

    rc = run_profile_command(client, task_ref=None, all_tasks=True, session_paths=session_paths)
    out = capsys.readouterr().out
    assert rc == 0
    assert "repo panopticon (2 tasks profiled)" in out
    assert "tarot" not in out  # zero profiled tasks for tarot -> no section at all


def test_run_profile_command_all_tasks_with_nothing_profiled(capsys: Any) -> None:
    client = _FakeClient(tasks=[{"id": "t1", "slug": "a", "repo_id": "r1"}], repos=[])
    rc = run_profile_command(client, task_ref=None, all_tasks=True, session_paths=lambda t: [])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No tasks with session transcripts found." in out
