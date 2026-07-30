"""Render a :func:`panopticon.profiler.parse.profile_transcripts` profile dict for a human, and
drive the ``panopticon profile`` CLI subcommand end to end.

Three renderers share the same duration/percentage formatting: :func:`format_task_profile` (the
``panopticon profile <task-id>`` compact breakdown), :func:`format_all_tasks_report` (the
``--all-tasks`` per-repo totals/medians), and :func:`format_time_summary` (the one-line dashboard
detail-pane summary). :func:`run_profile_command` wires them to the task service + the volume
reader — kept out of ``terminal/__main__.py`` per its existing pattern (``doctor.py``,
``quickstart.py``) of one module per non-trivial subcommand.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.profiler.categories import TOOL_CATEGORIES
from panopticon.profiler.parse import profile_transcripts
from panopticon.sessionservice.transcripts import task_session_paths


def _fmt_duration(seconds: float) -> str:
    """``'14h 32m'``/``'12m 03s'``/``'45s'`` — hours+minutes once over an hour (seconds dropped),
    otherwise minutes+seconds, otherwise bare seconds. One formatter, used everywhere in this
    module so every duration in the output is spelled the same way."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def _pct(part: float, whole: float) -> str:
    if whole <= 0:
        return "0%"
    return f"{round(100 * part / whole)}%"


def _median(values: Iterable[float]) -> float:
    values = list(values)
    return statistics.median(values) if values else 0.0


def _category_line(
    name: str, seconds: float, count: int, denom: float, *, indent: str = "    "
) -> str:
    return (
        f"{indent}{name:<14}{_fmt_duration(seconds):>10}  ({_pct(seconds, denom):>4})  "
        f"{count} call{'s' if count != 1 else ''}"
    )


def _top_line(
    label: str, seconds: float, denom: float | None = None, *, extra: str = "", width: int = 19
) -> str:
    """One of the top-level summary lines (``total wall``/``operator wait``/…), label left-padded
    to ``width`` so every value column lines up regardless of label length. ``denom`` omitted
    (``total wall`` itself, always 100%) skips the percentage."""
    pct = f"  ({_pct(seconds, denom)})" if denom is not None else ""
    return f"  {label + ':':<{width}}{_fmt_duration(seconds):>10}{pct}{extra}"


def format_task_profile(
    task: JsonObj, profile: dict[str, Any], *, repo_name: str | None = None
) -> str:
    """The ``panopticon profile <task-id>`` compact breakdown: total wall, operator-wait,
    between-sessions, then agent-active split by category, then the top 5 longest tool calls."""
    total = profile["total_wall_s"]
    active = profile["agent_active_s"]
    header = f"Task {task['id']} ({task.get('slug') or '-'})"
    if repo_name:
        header += f" — {repo_name}"
    lines = [
        header,
        _top_line("total wall", total),
        _top_line("operator wait", profile["operator_wait_s"], total),
    ]
    if profile["between_sessions_count"]:
        n = profile["between_sessions_count"]
        lines.append(
            _top_line(
                "between sessions",
                profile["between_sessions_s"],
                total,
                extra=f"   [{n} gap{'s' if n != 1 else ''}]",
            )
        )
    if profile["unattributed_s"] > 0:
        lines.append(_top_line("unattributed", profile["unattributed_s"], total))
    lines.append(_top_line("agent active", active, total))
    lines.append(_category_line("llm", profile["llm_s"], profile["llm_count"], active))
    for name in TOOL_CATEGORIES:
        bucket = profile["categories"][name]
        lines.append(_category_line(name, bucket["seconds"], bucket["count"], active))

    if profile["unmatched_tool_calls"]:
        n = profile["unmatched_tool_calls"]
        lines.append(
            f"  ({n} tool call{'s' if n != 1 else ''} never got a result — transcript cut off)"
        )

    top = profile["top_tool_calls"]
    if top:
        lines += ["", "  top 5 longest tool calls:"]
        for i, call in enumerate(top, start=1):
            lines.append(
                f"    {i}. {call['category']:<14}{_fmt_duration(call['duration_s']):>8}  {call['first_line']}"
            )
    return "\n".join(lines)


def format_time_summary(profile: dict[str, Any]) -> str:
    """The one-line dashboard detail-pane summary, e.g.
    ``agent 4.3h: llm 43% tests 19% tools 38% | waited on user 9.2h (+1.1h between sessions)``.

    Deliberately terse: only ``llm``/``tests`` are broken out (the operator's two named concerns —
    "waiting on the LLM" vs "waiting on tests"), everything else agent-active is lumped into
    ``tools``. The full per-category breakdown is the CLI's job (:func:`format_task_profile`)."""
    active = profile["agent_active_s"]
    llm_s = profile["llm_s"]
    tests_s = profile["categories"]["tests"]["seconds"]
    tools_s = max(0.0, active - llm_s - tests_s)
    hours = active / 3600
    summary = f"agent {hours:.1f}h: llm {_pct(llm_s, active)} tests {_pct(tests_s, active)} tools {_pct(tools_s, active)}"
    wait = f"waited on user {profile['operator_wait_s'] / 3600:.1f}h"
    if profile["between_sessions_s"] > 0:
        wait += f" (+{profile['between_sessions_s'] / 3600:.1f}h between sessions)"
    return f"{summary} | {wait}"


def aggregate_repo_profiles(profiles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Combine several tasks' profile dicts (all from one repo) into totals + medians — the input
    to :func:`format_all_tasks_report`. Sums answer "what fraction of time is X" questions;
    medians answer "how long does a typical task here take"."""
    n = len(profiles)
    if n == 0:
        return {"task_count": 0}
    totals = {
        "total_wall_s": sum(p["total_wall_s"] for p in profiles),
        "operator_wait_s": sum(p["operator_wait_s"] for p in profiles),
        "between_sessions_s": sum(p["between_sessions_s"] for p in profiles),
        "agent_active_s": sum(p["agent_active_s"] for p in profiles),
        "llm_s": sum(p["llm_s"] for p in profiles),
        "llm_count": sum(p["llm_count"] for p in profiles),
        "unattributed_s": sum(p["unattributed_s"] for p in profiles),
    }
    categories = {
        name: {
            "seconds": sum(p["categories"][name]["seconds"] for p in profiles),
            "count": sum(p["categories"][name]["count"] for p in profiles),
        }
        for name in TOOL_CATEGORIES
    }
    medians = {
        "total_wall_s": _median(p["total_wall_s"] for p in profiles),
        "operator_wait_s": _median(p["operator_wait_s"] for p in profiles),
        "agent_active_s": _median(p["agent_active_s"] for p in profiles),
    }
    return {"task_count": n, "totals": totals, "categories": categories, "medians": medians}


def format_all_tasks_report(
    per_repo: dict[str, dict[str, Any]], *, skipped: dict[str, int] | None = None
) -> str:
    """``panopticon profile --all-tasks``: one section per repo, sorted by name — median task
    shape, then each agent-active category as both a share of total agent-active time (the "what
    fraction of X's agent time is pytest" answer) and its raw total."""
    skipped = skipped or {}
    sections = []
    for repo_name in sorted(per_repo):
        agg = per_repo[repo_name]
        n = agg.get("task_count", 0)
        if n == 0:
            continue
        skip_n = skipped.get(repo_name, 0)
        totals, medians, categories = agg["totals"], agg["medians"], agg["categories"]
        active_total = totals["agent_active_s"]
        lines = [
            f"repo {repo_name} ({n} task{'s' if n != 1 else ''} profiled"
            + (f", {skip_n} skipped: no transcripts found)" if skip_n else ")"),
            _top_line("median wall", medians["total_wall_s"], width=23),
            _top_line(
                "median operator wait",
                medians["operator_wait_s"],
                medians["total_wall_s"],
                width=23,
            ),
            _top_line(
                "median agent active", medians["agent_active_s"], medians["total_wall_s"], width=23
            ),
            "",
            f"  agent-active time by category (of {_fmt_duration(active_total)} total across {n} tasks):",
            _category_line(
                "llm", totals["llm_s"], totals["llm_count"], active_total, indent="    "
            ),
        ]
        for name in TOOL_CATEGORIES:
            bucket = categories[name]
            lines.append(
                _category_line(
                    name, bucket["seconds"], bucket["count"], active_total, indent="    "
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _find_task(tasks: list[JsonObj], ref: str) -> JsonObj | None:
    return next((t for t in tasks if t.get("id") == ref or t.get("slug") == ref), None)


def run_profile_command(
    client: TaskServiceClient,
    *,
    task_ref: str | None,
    all_tasks: bool,
    session_paths: Callable[[str], list[Path]] = task_session_paths,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Drive ``panopticon profile <task-id-or-slug>`` / ``panopticon profile --all-tasks``.

    ``session_paths`` is injectable (defaults to the real docker-volume reader) so this is
    unit-testable without docker. Returns a process exit code; never raises for an ordinary
    "no such task"/"no transcripts" outcome — those print to ``stderr`` and return 1.

    ``stdout``/``stderr`` default to ``None`` and are resolved to ``sys.stdout``/``sys.stderr``
    *inside* the function body, not as parameter defaults — a default is bound once at import
    time, which would print past a test's ``capsys`` (it patches ``sys.stdout`` after import)."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    tasks = client.list_tasks()
    repo_names = {str(r["id"]): str(r["name"]) for r in client.list_repos()}

    if all_tasks:
        by_repo: dict[str, list[dict[str, Any]]] = {}
        skipped: dict[str, int] = {}
        for one_task in tasks:
            one_repo_name = repo_names.get(
                str(one_task.get("repo_id")), str(one_task.get("repo_id"))
            )
            paths = session_paths(str(one_task["id"]))
            if not paths:
                skipped[one_repo_name] = skipped.get(one_repo_name, 0) + 1
                continue
            by_repo.setdefault(one_repo_name, []).append(profile_transcripts(paths))
        if not by_repo:
            print("No tasks with session transcripts found.", file=stdout)
            return 0
        per_repo = {name: aggregate_repo_profiles(profiles) for name, profiles in by_repo.items()}
        print(format_all_tasks_report(per_repo, skipped=skipped), file=stdout)
        return 0

    if not task_ref:
        print("usage: panopticon profile <task-id-or-slug> | --all-tasks", file=stderr)
        return 2
    task = _find_task(tasks, task_ref)
    if task is None:
        print(f"No such task: {task_ref}", file=stderr)
        return 1
    paths = session_paths(str(task["id"]))
    if not paths:
        print(f"No session transcripts found for {task_ref}.", file=stderr)
        return 1
    profile = profile_transcripts(paths)
    repo_name = repo_names.get(str(task.get("repo_id")))
    print(format_task_profile(task, profile, repo_name=repo_name), file=stdout)
    return 0


__all__ = [
    "aggregate_repo_profiles",
    "format_all_tasks_report",
    "format_task_profile",
    "format_time_summary",
    "run_profile_command",
]
