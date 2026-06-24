"""TaskService orchestration: tasks, transition enforcement, slug, artifacts, liveness."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from panopticon.core import (
    Complete,
    IllegalTransition,
    InitialState,
    ResponsibilitiesNotMet,
    Workflow,
)
from panopticon.core.models import Actor, LifecyclePhase, Repo, Responsibility, Status
from panopticon.core.store import NotFound
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.taskservice.service import (
    AlreadyClaimed,
    NotAuthorized,
    TaskService,
    UnknownWorkflow,
)
from panopticon.workflows import GithubPeerReviewed, Orchestrator, Spike


def make_service(tmp_path: Path) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 10_000))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 10_000))
    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "github-peer-reviewed": GithubPeerReviewed(), "orchestrator": Orchestrator()},
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


def test_create_task_as_orchestrator_is_allowed(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    boss = svc.create_task("r1", "orchestrator")
    child = svc.create_task_as(boss.id, "github-peer-reviewed", memo="do a thing")
    assert child.workflow == "github-peer-reviewed"
    assert child.state == "PLANNING"  # the child's own workflow initial state
    assert child.memo == "do a thing"


def test_create_task_as_uses_the_orchestrators_own_repo(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    svc.create_repo(Repo(id="r2", name="acme/other", git_url="https://x/r2.git"))
    boss = svc.create_task("r2", "orchestrator")  # the orchestrator lives in r2
    child = svc.create_task_as(boss.id, "github-peer-reviewed")
    assert child.repo_id == "r2"  # first iteration: always the orchestrator's own repo


def test_create_task_as_non_orchestrator_is_rejected(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    actor = svc.create_task("r1", "spike")  # spike does not orchestrate
    with pytest.raises(NotAuthorized):
        svc.create_task_as(actor.id, "spike")
    assert len(svc.list_tasks()) == 1  # nothing created


def test_create_task_as_unknown_actor_is_not_found(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.create_task_as("ghost", "spike")


def test_gated_discovery_requires_orchestrator(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    boss = svc.create_task("r1", "orchestrator")
    spike = svc.create_task("r1", "spike")
    assert "orchestrator" in svc.workflow_names_as(boss.id)
    with pytest.raises(NotAuthorized):
        svc.workflow_names_as(spike.id)


def test_create_task_uses_engine_defaults(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    assert task.id == "id1"  # from the injected id factory
    assert task.state == "ITERATING"
    assert task.turn is Actor.USER  # initial state → turn starts with the user
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


# -- lifecycle hook: the service runs Workflow.on_transition on each transition ------


def test_skills_exposes_the_active_workflows_skills(tmp_path: Path) -> None:
    from panopticon.core.models import Skill

    class Skilled(Workflow):
        name = "skilled"

        class A(InitialState):
            label = "A"
            transitions = (Complete,)

        initial = A

        def skills(self) -> tuple[Skill, ...]:
            return (Skill("babysit-ci", "Watch CI.", "Do it."),)

    svc = TaskService(SqlAlchemyStore(), {"skilled": Skilled()}, FilesystemArtifactStore(tmp_path))
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = svc.create_task("r1", "skilled")
    skills = svc.skills(task.id)
    # The agnostic `provision` skill is exposed first, then the workflow's own skills.
    assert [s.name for s in skills] == ["provision", "babysit-ci"]


def test_provision_skill_is_exposed_even_for_skill_less_workflows(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")  # the seed workflow declares no skills of its own
    assert [s.name for s in svc.skills(task.id)] == ["provision"]


def test_on_transition_hook_fires_through_the_service(tmp_path: Path) -> None:
    calls: list[tuple[str | None, str]] = []

    class Hooked(Workflow):
        name = "hooked"

        class A(InitialState):
            label = "A"
            transitions = (Complete,)

        initial = A

        def on_transition(self, task, *, from_state, to_state, artifacts):  # type: ignore[override]
            calls.append((from_state, to_state))

    svc = TaskService(SqlAlchemyStore(), {"hooked": Hooked()}, FilesystemArtifactStore(tmp_path))
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = svc.create_task("r1", "hooked")
    svc.apply_operation(task.id, "advance")  # A -> COMPLETE
    assert calls == [("A", "COMPLETE")]


# -- turn-flip + blocked marker -----------------------------------------------------


def test_set_turn_flips_within_a_state(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")  # turn=USER on entry (initial state)
    flipped = svc.set_turn(task.id, Actor.USER)  # e.g. the agent asked a question
    assert flipped.turn is Actor.USER
    assert svc.get_task(task.id).turn is Actor.USER
    assert svc.set_turn(task.id, Actor.AGENT).turn is Actor.AGENT  # user replied


def test_blocked_marker_survives_turn_flips(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.set_blocked(task.id, True)
    svc.set_turn(task.id, Actor.USER)  # a flip must not clear the deliberate block
    reloaded = svc.get_task(task.id)
    assert reloaded.turn is Actor.USER
    assert reloaded.blocked is True
    assert svc.set_blocked(task.id, False).blocked is False  # cleared only explicitly


# -- claim: a runner owns the task (the spawn gate) ---------------------------------


def test_claim_is_compare_and_set_and_idempotent_for_the_holder(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    assert task.claimed_by is None
    assert svc.claim(task.id, "host-1").claimed_by == "host-1"
    assert svc.claim(task.id, "host-1").claimed_by == "host-1"  # idempotent for the same runner
    assert svc.get_task(task.id).claimed_by == "host-1"  # persisted


def test_claim_rejects_a_different_runner(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.claim(task.id, "host-1")
    with pytest.raises(AlreadyClaimed):
        svc.claim(task.id, "host-2")
    assert svc.get_task(task.id).claimed_by == "host-1"  # unchanged


def test_release_returns_the_task_to_unclaimed(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.claim(task.id, "host-1")
    assert svc.release(task.id).claimed_by is None
    assert svc.claim(task.id, "host-2").claimed_by == "host-2"  # now another runner can claim


# -- host (runner) liveness + reclaim: connection-scoped, clock-free (mirror of container liveness) --


def test_register_runner_is_live_until_deregistered(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    assert svc.live_runners() == set()
    reg = svc.register_runner("host-1")
    assert svc.live_runners() == {"host-1"}  # a held connection => the runner is live
    svc.deregister_runner(reg.id)
    assert svc.live_runners() == set()  # dropped connection => no longer live (no clock read)


def test_register_runner_reconnect_overlap_keeps_the_runner_live(tmp_path: Path) -> None:
    # A reconnect during a blip can briefly hold two connections; the *old* one's disconnect must
    # not drop the runner while the *new* one is up (each connection has its own id, not keyed by
    # runner_id), so the runner stays continuously live across the reconnect.
    svc = make_service(tmp_path)
    old = svc.register_runner("host-1")
    svc.register_runner("host-1")  # the reconnect opens before the old finally fires
    svc.deregister_runner(old.id)  # the old connection's disconnect lands late
    assert svc.live_runners() == {"host-1"}  # still live on the fresh connection


def test_reclaim_releases_the_runners_non_terminal_claims(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    mine = svc.create_task("r1", "spike")
    other = svc.create_task("r1", "spike")
    svc.claim(mine.id, "host-dead")
    svc.claim(other.id, "host-live")

    reclaimed = svc.reclaim("host-dead")

    assert [t.id for t in reclaimed] == [mine.id]
    assert svc.get_task(mine.id).claimed_by is None  # released for a healthy host to respawn
    assert svc.get_task(other.id).claimed_by == "host-live"  # another runner's claim untouched
    assert svc.reclaim("host-dead") == []  # idempotent


def test_reclaim_skips_terminal_tasks(tmp_path: Path) -> None:
    # A terminal task has nothing to respawn, so reclaim leaves its claim alone (no churn).
    svc = make_service(tmp_path)
    done = svc.create_task("r1", "spike")
    svc.claim(done.id, "host-dead")
    svc.apply_operation(done.id, "drop")  # -> DROPPED (terminal)

    assert svc.reclaim("host-dead") == []
    assert svc.get_task(done.id).claimed_by == "host-dead"  # unchanged


# -- container lifecycle: the session service's reported spawn phase, folded into a status ---------


def test_container_status_folds_phase_registration_and_runner_liveness(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    status = lambda: svc.container_status(svc.get_task(task.id)).value  # noqa: E731

    assert status() == "queued"  # unclaimed, non-terminal → waiting for a runner
    svc.claim(task.id, "host-1")
    svc.register_runner("host-1")  # the runner holds its host-liveness connection
    svc.report_lifecycle(task.id, "host-1", LifecyclePhase.BUILDING, "gh + uv")
    assert status() == "building"  # the reported spawn phase shows through
    reg = svc.register(task.id, "c1", "host-1")
    assert status() == "live"  # an open container registration trumps the phase
    svc.deregister(reg.id)
    assert status() == "building"  # container gone but phase still in flight (reconcile not yet run)
    svc.clear_lifecycle(task.id)  # the daemon's reconcile clears the stale phase
    assert status() == "down"  # claimed + runner live + no phase + no registration → down


def test_container_status_is_disconnected_when_the_claiming_runner_is_gone(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.claim(task.id, "host-1")
    svc.report_lifecycle(task.id, "host-1", LifecyclePhase.AWAITING)
    # no runner-liveness connection held → claimed by a runner not connected to the task service
    assert svc.container_status(svc.get_task(task.id)).value == "disconnected"
    svc.apply_operation(task.id, "drop")  # terminal → no container concept
    assert svc.container_status(svc.get_task(task.id)).value == "–"


def test_lifecycle_phase_is_cleared_on_release_and_reclaim(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.claim(task.id, "host-1")
    svc.report_lifecycle(task.id, "host-1", LifecyclePhase.AWAITING)
    assert svc.lifecycle(task.id) is not None
    svc.release(task.id)
    assert svc.lifecycle(task.id) is None  # a respawn starts clean

    svc.claim(task.id, "host-2")
    svc.report_lifecycle(task.id, "host-2", LifecyclePhase.BUILDING)
    svc.reclaim("host-2")  # a dead runner's phase is stale
    assert svc.lifecycle(task.id) is None


def test_ephemeral_changes_bump_the_change_feed_version(tmp_path: Path) -> None:
    # The dashboard's long-poll only wakes on a version change, so ephemeral liveness events
    # (a phase advancing, a container going live, a runner connecting) must bump it too.
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")

    v = svc.tasks_version()
    svc.report_lifecycle(task.id, "host-1", LifecyclePhase.BUILDING)
    assert svc.tasks_version() > v  # a reported phase wakes the feed

    v = svc.tasks_version()
    reg = svc.register(task.id, "c1", "host-1")
    assert svc.tasks_version() > v  # a container going live wakes it
    v = svc.tasks_version()
    svc.deregister(reg.id)
    assert svc.tasks_version() > v  # a container dropping wakes it

    v = svc.tasks_version()
    runner = svc.register_runner("host-1")
    assert svc.tasks_version() > v  # a runner connecting wakes it
    svc.deregister_runner(runner.id)

    # clearing a present phase wakes the feed; clearing an absent one is a no-op (no spurious wake)
    svc.report_lifecycle(task.id, "host-1", LifecyclePhase.STARTING)
    v = svc.tasks_version()
    svc.clear_lifecycle(task.id)
    assert svc.tasks_version() > v
    steady = svc.tasks_version()
    svc.clear_lifecycle(task.id)
    assert svc.tasks_version() == steady


# -- provisioning: the session service does the host git; the service only records it (ADR 0010) --


def test_record_provisioning_records_the_refs(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.set_slug(task.id, "fix-widget")
    out = svc.record_provisioning(
        task.id, branch="panopticon/fix-widget", clone="/clones/id1"
    )
    assert (out.branch, out.clone) == ("panopticon/fix-widget", "/clones/id1")
    reloaded = svc.get_task(task.id)  # a pure recorded-fact write; it persisted
    assert (reloaded.branch, reloaded.clone) == ("panopticon/fix-widget", "/clones/id1")


def test_record_provisioning_is_slug_gated(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")  # no slug yet — the branch is named from the slug
    with pytest.raises(ValueError, match="slug"):
        svc.record_provisioning(task.id, branch="panopticon/x", clone="/clones/x")
    assert svc.get_task(task.id).branch is None


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


def test_set_slug_aliases_the_artifacts_dir(tmp_path: Path) -> None:
    # Setting the slug exposes the task's artifacts under <root>/tasks/<slug> (the symlink).
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.put_artifact(task.id, "plan.md", b"# Plan\n")
    svc.set_slug(task.id, "fix-widget")
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink()
    assert (alias / "plan.md").read_bytes() == b"# Plan\n"


def test_re_slug_swaps_the_alias(tmp_path: Path) -> None:
    # Re-slugging drops the stale alias and points a fresh one at the same task.
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    svc.put_artifact(task.id, "plan.md", b"# Plan\n")
    svc.set_slug(task.id, "old-name")
    svc.set_slug(task.id, "new-name")
    assert not (tmp_path / "tasks" / "old-name").exists()
    assert (tmp_path / "tasks" / "new-name" / "plan.md").read_bytes() == b"# Plan\n"


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


def test_briefing_surfaces_the_plan_uri_once_the_plan_exists(tmp_path: Path) -> None:
    # The briefing names the plan's canonical URI only after the plan.md artifact is written, so the
    # agent reads it back at the right URI instead of guessing.
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "github-peer-reviewed")
    assert "panopticon://" not in svc.briefing(task.id)  # no plan yet → no URI
    svc.put_artifact(task.id, "plan.md", b"# Plan")
    assert f"panopticon://tasks/{task.id}/artifacts/plan.md" in svc.briefing(task.id)


# -- liveness -----------------------------------------------------------------------


def test_register_deregister(tmp_path: Path) -> None:
    # Liveness is connection-scoped: a registration lives exactly as long as the container holds
    # its `/live` connection. register == connect; deregister == disconnect. No heartbeat, no TTL.
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "spike")
    reg = svc.register(task.id, container_id="c-abc", runner_id="runner-1")
    assert reg.task_id == task.id
    assert [r.id for r in svc.registrations(task.id)] == [reg.id]

    svc.deregister(reg.id)
    assert svc.registrations(task.id) == []


def test_register_requires_task(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        svc.register("ghost", container_id="c-abc")


def test_registrations_do_not_age_out_on_the_clock(tmp_path: Path) -> None:
    # The old model reaped registrations on a wall-clock TTL; the connection model never does — a
    # registration is removed only by an explicit deregister (the dropped connection), so reading
    # `registrations` touches no clock no matter how much time passes.
    now = {"t": "2026-01-01T00:00:00+00:00"}
    svc = TaskService(
        SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path),
        clock=lambda: now["t"],
    )
    svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = svc.create_task("r1", "spike")
    reg = svc.register(task.id, container_id="c-abc")

    now["t"] = "2026-01-01T01:00:00+00:00"  # +1h — would be long past any old TTL
    assert [r.id for r in svc.registrations(task.id)] == [reg.id]  # still live: only disconnect reaps


# -- responsibilities ---------------------------------------------------------------


class _Gated(Workflow):
    name = "gated"

    class Working(InitialState):
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
    task = svc.create_task("r1", "github-peer-reviewed")  # PLANNING, plan-written unmet
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
    task = svc.create_task("r1", "github-peer-reviewed")
    assert set(svc.workflow_states(task.id)) == {
        "PLANNING", "ITERATING", "REVIEW", "MERGING", "COMPLETE", "DROPPED",
    }


def test_going_back_to_coding_uses_set_state_not_an_operation(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    task = svc.create_task("r1", "github-peer-reviewed")
    svc.set_state(task.id, "REVIEW")  # jump to REVIEW (pr-reviewed now PENDING)
    assert "iterate" not in svc.operations(task.id)  # no such operation
    svc.set_state(task.id, "ITERATING")  # free move back to coding, despite the unmet promise
    assert svc.get_task(task.id).state == "ITERATING"
