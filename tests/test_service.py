"""TaskService orchestration: tasks, transition enforcement, slug, artifacts, liveness."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from panopticon.core import (
    Complete,
    IllegalTransition,
    ResponsibilitiesNotMet,
    State,
    Workflow,
)
from panopticon.core.models import Actor, Repo, Responsibility, Status
from panopticon.core.store import NotFound
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.service import TaskService, UnknownWorkflow
from panopticon.workflows import Parity, Spike


def make_service(tmp_path: Path) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 10_000))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 10_000))
    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "parity": Parity()},
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


def test_create_task_uses_engine_defaults(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    assert task.id == "id1"  # from the injected id factory
    assert task.state == "ITERATING"
    assert task.turn is Actor.AGENT
    assert task.slug is None


def test_create_task_unknown_workflow(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(UnknownWorkflow):
        svc.create_task("r1", "nope")


def test_create_task_missing_repo(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.create_task("ghost", "spike")


def test_get_missing_task(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.get_task("ghost")


def test_legal_transition_persists(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.request_transition(task.id, "COMPLETE", trigger="finish")
    reloaded = svc.get_task(task.id)
    assert reloaded.state == "COMPLETE"
    assert [h.to_state for h in reloaded.history] == ["ITERATING", "COMPLETE"]


def test_illegal_transition_rejected(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    with pytest.raises(IllegalTransition):
        svc.request_transition(task.id, "WORK")  # not a free-form state


def test_set_slug(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.set_slug(task.id, "fix-widget")
    assert svc.get_task(task.id).slug == "fix-widget"


# -- artifacts ----------------------------------------------------------------------


def test_artifacts_require_task(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.put_artifact("ghost", "plan.md", b"x")


def test_artifact_roundtrip(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.put_artifact(task.id, "plan.md", b"# Plan")
    assert svc.get_artifact(task.id, "plan.md") == b"# Plan"
    assert svc.list_artifacts(task.id) == ["plan.md"]


# -- liveness -----------------------------------------------------------------------


def test_register_heartbeat_deregister(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    reg = svc.register(task.id, container_id="c-abc", runner_id="runner-1")
    assert reg.task_id == task.id
    assert [r.id for r in svc.registrations(task.id)] == [reg.id]

    before = reg.last_seen
    updated = svc.heartbeat(reg.id)
    assert updated.last_seen != before  # clock advanced

    svc.deregister(reg.id)
    assert svc.registrations(task.id) == []


def test_register_requires_task(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.register("ghost", container_id="c-abc")


def test_heartbeat_unknown_registration(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.heartbeat("nope")


# -- responsibilities ---------------------------------------------------------------


class _Gated(Workflow):
    name = "gated"

    class Working(State):
        label = "WORKING"
        responsibilities = (Responsibility(key="tests-pass", description="Tests pass"),)
        transitions = (Complete,)

    initial = Working


def make_gated_service(tmp_path: Path) -> TaskService:
    svc = TaskService(
        SqlAlchemyStore(),
        {"gated": _Gated()},
        FilesystemArtifactStore(tmp_path),
    )
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


def test_resolve_responsibility_unblocks_transition(tmp_path: Path) -> None:
    svc = make_gated_service(tmp_path)
    task = svc.create_task("r1", "gated")  # starts in WORKING, promise PENDING
    with pytest.raises(ResponsibilitiesNotMet):
        svc.request_transition(task.id, "COMPLETE")
    svc.resolve_responsibility(task.id, "tests-pass", status=Status.MET)
    done = svc.request_transition(task.id, "COMPLETE")
    assert done.state == "COMPLETE"


def test_report_unknown_responsibility_rejected(tmp_path: Path) -> None:
    svc = make_gated_service(tmp_path)
    task = svc.create_task("r1", "gated")
    with pytest.raises(ValueError):
        svc.resolve_responsibility(task.id, "ghost", status=Status.MET)


# -- free state override (the user can move freely) + free operations ----------------


def test_set_state_is_a_free_move_off_graph_and_ungated(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "parity")  # PLANNING, plan-written unmet
    # Skip straight to MERGING — not a legal transition, and the gate is unmet — yet it succeeds.
    moved = svc.set_state(task.id, "MERGING")
    assert moved.state == "MERGING"
    assert svc.get_task(task.id).history[-1].trigger == "set-state"


def test_set_state_can_reopen_a_terminal_task(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.request_transition(task.id, "COMPLETE")  # terminal
    svc.set_state(task.id, "ITERATING")  # the user can move even out of a terminal
    assert svc.get_task(task.id).state == "ITERATING"


def test_workflow_states_lists_every_state(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "parity")
    assert set(svc.workflow_states(task.id)) == {
        "PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE", "DROPPED",
    }


def test_going_back_to_coding_uses_set_state_not_an_operation(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "parity")
    svc.set_state(task.id, "REVIEW")  # jump to REVIEW (pr-reviewed now PENDING)
    assert "iterate" not in svc.operations(task.id)  # no such operation
    svc.set_state(task.id, "ITERATING")  # free move back to coding, despite the unmet promise
    assert svc.get_task(task.id).state == "ITERATING"
