"""Unit tests for ``Task``'s own behavior — fulfilling promises and reporting progress.

These exercise the task in isolation (a hand-built history, no ``Workflow``): a task knows how
to update its own record regardless of how it got into a state. The state-machine rules that
*gate* transitions live in :mod:`test_workflow`.
"""

from __future__ import annotations

import pytest

from panopticon.core import HistoryEntry, Responsibility, Status, Task
from panopticon.core.models import Actor


def _working_task() -> Task:
    """A task in WORKING whose entry carries two PENDING promises (no Workflow involved)."""
    return Task(
        id="t1",
        repo_id="r1",
        workflow="gated",
        state="WORKING",
        turn=Actor.AGENT,
        history=[
            HistoryEntry(at="t0", from_state=None, to_state="PLAN"),
            HistoryEntry(
                at="t1",
                from_state="PLAN",
                to_state="WORKING",
                responsibilities=[
                    Responsibility(key="tests-pass", description="Tests pass"),
                    Responsibility(key="pr-opened", description="PR opened"),
                ],
            ),
        ],
    )


def test_current_entry_is_the_latest() -> None:
    task = _working_task()
    assert task.current_entry is task.history[-1]
    assert task.current_entry.to_state == "WORKING"


def test_blocked_defaults_false() -> None:
    assert _working_task().blocked is False  # the deliberate marker starts clear


def test_depends_on_defaults_to_empty_list() -> None:
    assert _working_task().depends_on == []


def test_provisioned_reflects_the_branch() -> None:
    task = _working_task()
    assert task.provisioned is False  # no branch yet
    task.branch = "panopticon/fix-widget"
    assert task.provisioned is True  # provisioned once the branch is recorded


def test_resolve_responsibility_fulfils_in_place() -> None:
    task = _working_task()
    entry = task.current_entry
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    by_key = {r.key: r for r in entry.responsibilities}  # same entry object, mutated
    assert by_key["tests-pass"].status is Status.MET
    assert by_key["tests-pass"].description == "Tests pass"  # definition text preserved
    assert by_key["pr-opened"].status is Status.PENDING  # untouched


def test_outstanding_responsibilities_tracks_progress() -> None:
    task = _working_task()
    assert {r.key for r in task.outstanding_responsibilities} == {"tests-pass", "pr-opened"}
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    assert {r.key for r in task.outstanding_responsibilities} == {"pr-opened"}
    task.resolve_responsibility(key="pr-opened", status=Status.FAILED, comment="forge down")
    assert task.outstanding_responsibilities == []  # FAILED-with-comment is resolved


def test_resolve_unknown_responsibility_is_rejected() -> None:
    task = _working_task()
    with pytest.raises(ValueError):
        task.resolve_responsibility(key="ghost", status=Status.MET)


def test_resolve_pending_is_rejected() -> None:
    task = _working_task()
    with pytest.raises(ValueError):
        task.resolve_responsibility(key="tests-pass", status=Status.PENDING)


def test_failed_requires_comment() -> None:
    task = _working_task()
    with pytest.raises(ValueError):
        task.resolve_responsibility(key="pr-opened", status=Status.FAILED)  # no comment
