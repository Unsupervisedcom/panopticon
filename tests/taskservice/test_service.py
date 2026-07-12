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
from panopticon.taskservice.service import (
    AlreadyClaimed,
    NotAuthorized,
    TaskService,
    UnknownWorkflow,
)
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import GithubPeerReviewed, Orchestrator, Spike


async def make_service(tmp_path: Path) -> TaskService:
    ids: Iterator[str] = iter(f"id{i}" for i in range(1, 10_000))
    times: Iterator[str] = iter(f"t{i}" for i in range(1, 10_000))
    svc = TaskService(
        SqlAlchemyStore(),
        {
            "spike": Spike(),
            "github-peer-reviewed": GithubPeerReviewed(),
            "orchestrator": Orchestrator(),
        },
        FilesystemArtifactStore(tmp_path),
        clock=lambda: next(times),
        id_factory=lambda: next(ids),
    )
    await svc.init()
    await svc.create_repo(
        Repo(
            id="r1",
            name="acme/widgets",
            git_url="https://x/r1.git",
            enabled_workflows=["github-peer-reviewed"],
        )
    )
    return svc


async def test_create_task_as_orchestrator_is_allowed(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    child = await svc.create_task_as(boss.id, "github-peer-reviewed", memo="do a thing")
    assert child.workflow == "github-peer-reviewed"
    assert child.state == "PLANNING"  # the child's own workflow initial state
    assert child.memo == "do a thing"


async def test_create_task_as_uses_the_orchestrators_own_repo(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    await svc.create_repo(
        Repo(
            id="r2",
            name="acme/other",
            git_url="https://x/r2.git",
            enabled_workflows=["github-peer-reviewed"],
        )
    )
    boss = await svc.create_task("r2", "orchestrator")  # the orchestrator lives in r2
    child = await svc.create_task_as(boss.id, "github-peer-reviewed")
    assert child.repo_id == "r2"  # first iteration: always the orchestrator's own repo


async def test_create_task_as_non_orchestrator_is_rejected(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    actor = await svc.create_task("r1", "spike")  # spike does not orchestrate
    with pytest.raises(NotAuthorized):
        await svc.create_task_as(actor.id, "spike")
    assert len(await svc.list_tasks()) == 1  # nothing created


async def test_create_task_as_unknown_actor_is_not_found(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.create_task_as("ghost", "spike")


async def test_gated_discovery_requires_orchestrator(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    boss = await svc.create_task("r1", "orchestrator")
    spike = await svc.create_task("r1", "spike")
    assert "orchestrator" in await svc.workflow_names_as(boss.id)
    with pytest.raises(NotAuthorized):
        await svc.workflow_names_as(spike.id)


async def test_create_task_uses_engine_defaults(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    assert task.id == "id1"  # from the injected id factory
    assert task.state == "ITERATING"
    assert task.turn is Actor.USER  # initial state → turn starts with the user
    assert task.slug is None


async def test_create_task_unknown_workflow(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(UnknownWorkflow):
        await svc.create_task("r1", "nope")


async def test_create_task_opt_in_workflow_not_enabled_is_rejected(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    # spike is opt-out so it is always allowed; github-peer-reviewed is opt-in and IS enabled
    # on r1 (via make_service). Create a repo without it enabled to verify the gate.
    await svc.create_repo(Repo(id="r2", name="acme/other", git_url="https://x/r2.git"))
    with pytest.raises(NotAuthorized, match="github-peer-reviewed"):
        await svc.create_task("r2", "github-peer-reviewed")


async def test_create_task_opt_out_workflow_is_allowed_by_default(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")  # spike is opt-out → always visible
    assert task.workflow == "spike"


async def test_create_task_opt_out_workflow_can_be_disabled(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    await svc.create_repo(
        Repo(id="r2", name="acme/other", git_url="https://x/r2.git", disabled_workflows=["spike"])
    )
    with pytest.raises(NotAuthorized, match="spike"):
        await svc.create_task("r2", "spike")


async def test_list_workflow_infos_for_repo_shows_opt_in_and_filters(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    # r1 has github-peer-reviewed enabled, spike is opt-out → both visible
    infos = await svc.list_workflow_infos_for_repo("r1")
    names = {w["name"] for w in infos}
    assert "spike" in names
    assert "github-peer-reviewed" in names
    # All infos carry opt_in and auto_submit_memo
    assert all("opt_in" in w for w in infos)
    assert all("auto_submit_memo" in w for w in infos)


async def test_list_workflow_infos_for_repo_hides_disabled_opt_out(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    await svc.create_repo(
        Repo(id="r2", name="acme/other", git_url="https://x/r2.git", disabled_workflows=["spike"])
    )
    names = {w["name"] for w in await svc.list_workflow_infos_for_repo("r2")}
    assert "spike" not in names


async def test_create_task_missing_repo(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.create_task("ghost", "spike")


async def test_get_missing_task(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.get_task("ghost")


async def test_legal_transition_persists(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.request_transition(task.id, "COMPLETE", trigger="finish")
    reloaded = await svc.get_task(task.id)
    assert reloaded.state == "COMPLETE"
    assert [h.to_state for h in reloaded.history] == ["ITERATING", "COMPLETE"]


# -- lifecycle hook: the service runs Workflow.on_transition on each transition ------


async def test_skills_exposes_the_active_workflows_skills(tmp_path: Path) -> None:
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
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await svc.create_task("r1", "skilled")
    skills = await svc.skills(task.id)
    # The agnostic `provision` skill is exposed first, then the workflow's own skills.
    assert [s.name for s in skills] == ["provision", "babysit-ci"]


async def test_provision_skill_is_exposed_even_for_skill_less_workflows(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")  # the seed workflow declares no skills of its own
    assert [s.name for s in await svc.skills(task.id)] == ["provision"]


async def test_on_transition_hook_fires_through_the_service(tmp_path: Path) -> None:
    calls: list[tuple[str | None, str]] = []

    class Hooked(Workflow):
        name = "hooked"

        class A(InitialState):
            label = "A"
            transitions = (Complete,)

        initial = A

        async def on_transition(self, task, *, from_state, to_state, artifacts):  # type: ignore[override]
            calls.append((from_state, to_state))

    svc = TaskService(SqlAlchemyStore(), {"hooked": Hooked()}, FilesystemArtifactStore(tmp_path))
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await svc.create_task("r1", "hooked")
    await svc.apply_operation(task.id, "advance")  # A -> COMPLETE
    assert calls == [("A", "COMPLETE")]


# -- turn-flip + blocked marker -----------------------------------------------------


async def test_set_turn_flips_within_a_state(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")  # turn=USER on entry (initial state)
    flipped = await svc.set_turn(task.id, Actor.USER)  # e.g. the agent asked a question
    assert flipped.turn is Actor.USER
    assert (await svc.get_task(task.id)).turn is Actor.USER
    assert (await svc.set_turn(task.id, Actor.AGENT)).turn is Actor.AGENT  # user replied


async def test_blocked_marker_survives_turn_flips(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.set_blocked(task.id, True)
    await svc.set_turn(task.id, Actor.USER)  # a flip must not clear the deliberate block
    reloaded = await svc.get_task(task.id)
    assert reloaded.turn is Actor.USER
    assert reloaded.blocked is True
    assert (await svc.set_blocked(task.id, False)).blocked is False  # cleared only explicitly


# -- claim: a runner owns the task (the spawn gate) ---------------------------------


async def test_claim_is_compare_and_set_and_idempotent_for_the_holder(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    assert task.claimed_by is None
    assert (await svc.claim(task.id, "host-1")).claimed_by == "host-1"
    assert (
        await svc.claim(task.id, "host-1")
    ).claimed_by == "host-1"  # idempotent for the same runner
    assert (await svc.get_task(task.id)).claimed_by == "host-1"  # persisted


async def test_claim_rejects_a_different_runner(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-1")
    with pytest.raises(AlreadyClaimed):
        await svc.claim(task.id, "host-2")
    assert (await svc.get_task(task.id)).claimed_by == "host-1"  # unchanged


async def test_release_returns_the_task_to_unclaimed(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-1")
    assert (await svc.release(task.id)).claimed_by is None
    assert (
        await svc.claim(task.id, "host-2")
    ).claimed_by == "host-2"  # now another runner can claim


# -- host (runner) liveness + reclaim: connection-scoped, clock-free (mirror of container liveness) --


async def test_register_runner_is_live_until_deregistered(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    assert svc.live_runners() == set()
    reg = await svc.register_runner("host-1")
    assert svc.live_runners() == {"host-1"}  # a held connection => the runner is live
    await svc.deregister_runner(reg.id)
    assert svc.live_runners() == set()  # dropped connection => no longer live (no clock read)


async def test_register_runner_with_host_is_surfaced_by_runner_host(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    assert svc.runner_host("host-1") is None  # unknown runner
    await svc.register_runner("host-1", host="box.example.com")
    assert svc.runner_host("host-1") == "box.example.com"
    # live_runner_registrations returns one entry per distinct runner id.
    regs = svc.live_runner_registrations()
    assert len(regs) == 1
    assert regs[0].runner_id == "host-1" and regs[0].host == "box.example.com"


async def test_register_runner_reconnect_overlap_keeps_the_runner_live(tmp_path: Path) -> None:
    # A reconnect during a blip can briefly hold two connections; the *old* one's disconnect must
    # not drop the runner while the *new* one is up (each connection has its own id, not keyed by
    # runner_id), so the runner stays continuously live across the reconnect.
    svc = await make_service(tmp_path)
    old = await svc.register_runner("host-1")
    await svc.register_runner("host-1")  # the reconnect opens before the old finally fires
    await svc.deregister_runner(old.id)  # the old connection's disconnect lands late
    assert svc.live_runners() == {"host-1"}  # still live on the fresh connection


async def test_reclaim_releases_the_runners_non_terminal_claims(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    mine = await svc.create_task("r1", "spike")
    other = await svc.create_task("r1", "spike")
    await svc.claim(mine.id, "host-dead")
    await svc.claim(other.id, "host-live")

    reclaimed = await svc.reclaim("host-dead")

    assert [t.id for t in reclaimed] == [mine.id]
    assert (
        await svc.get_task(mine.id)
    ).claimed_by is None  # released for a healthy host to respawn
    assert (
        await svc.get_task(other.id)
    ).claimed_by == "host-live"  # another runner's claim untouched
    assert await svc.reclaim("host-dead") == []  # idempotent


async def test_reclaim_skips_terminal_tasks(tmp_path: Path) -> None:
    # A terminal task has nothing to respawn, so reclaim leaves its claim alone (no churn).
    svc = await make_service(tmp_path)
    done = await svc.create_task("r1", "spike")
    await svc.claim(done.id, "host-dead")
    await svc.apply_operation(done.id, "drop")  # -> DROPPED (terminal)

    assert await svc.reclaim("host-dead") == []
    assert (await svc.get_task(done.id)).claimed_by == "host-dead"  # unchanged


async def test_claim_does_not_bump_updated_at(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    before = task.updated_at
    await svc.claim(task.id, "host-1")
    assert (await svc.get_task(task.id)).updated_at == before


async def test_release_does_not_bump_updated_at(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-1")
    before = (await svc.get_task(task.id)).updated_at
    await svc.release(task.id)
    assert (await svc.get_task(task.id)).updated_at == before


async def test_reclaim_does_not_bump_updated_at(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-dead")
    before = (await svc.get_task(task.id)).updated_at
    await svc.reclaim("host-dead")
    assert (await svc.get_task(task.id)).updated_at == before


# -- container lifecycle: the session service's reported spawn phase, folded into a status ---------


async def test_container_status_folds_phase_registration_and_runner_liveness(
    tmp_path: Path,
) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")

    async def status() -> str:
        return svc.container_status(await svc.get_task(task.id)).value

    assert await status() == "queued"  # unclaimed, non-terminal → waiting for a runner
    await svc.claim(task.id, "host-1")
    await svc.register_runner("host-1")  # the runner holds its host-liveness connection
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.BUILDING, "gh + uv")
    assert await status() == "building"  # the reported spawn phase shows through
    reg = await svc.register(task.id, "c1", "host-1")
    assert await status() == "live"  # an open container registration trumps the phase
    await svc.deregister(reg.id)
    assert (
        await status() == "building"
    )  # container gone but phase still in flight (reconcile not yet run)
    svc.clear_lifecycle(task.id)  # the daemon's reconcile clears the stale phase
    assert await status() == "down"  # claimed + runner live + no phase + no registration → down


async def test_container_status_is_disconnected_when_the_claiming_runner_is_gone(
    tmp_path: Path,
) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-1")
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.AWAITING)
    # no runner-liveness connection held → claimed by a runner not connected to the task service
    assert svc.container_status(await svc.get_task(task.id)).value == "disconnected"
    await svc.apply_operation(task.id, "drop")  # terminal → no container concept
    assert svc.container_status(await svc.get_task(task.id)).value == "–"


async def test_lifecycle_phase_is_cleared_on_release_and_reclaim(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.claim(task.id, "host-1")
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.AWAITING)
    assert svc.lifecycle(task.id) is not None
    await svc.release(task.id)
    assert svc.lifecycle(task.id) is None  # a respawn starts clean

    await svc.claim(task.id, "host-2")
    await svc.report_lifecycle(task.id, "host-2", LifecyclePhase.BUILDING)
    await svc.reclaim("host-2")  # a dead runner's phase is stale
    assert svc.lifecycle(task.id) is None


async def test_ephemeral_changes_bump_the_change_feed_version(tmp_path: Path) -> None:
    # The dashboard's long-poll only wakes on a version change, so ephemeral liveness events
    # (a phase advancing, a container going live, a runner connecting) must bump it too.
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")

    v = svc.tasks_version()
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.BUILDING)
    assert svc.tasks_version() > v  # a reported phase wakes the feed

    v = svc.tasks_version()
    reg = await svc.register(task.id, "c1", "host-1")
    assert svc.tasks_version() > v  # a container going live wakes it
    v = svc.tasks_version()
    await svc.deregister(reg.id)
    assert svc.tasks_version() > v  # a container dropping wakes it

    v = svc.tasks_version()
    runner = await svc.register_runner("host-1")
    assert svc.tasks_version() > v  # a runner connecting wakes it
    await svc.deregister_runner(runner.id)

    # clearing a present phase wakes the feed; clearing an absent one is a no-op (no spurious wake)
    await svc.report_lifecycle(task.id, "host-1", LifecyclePhase.STARTING)
    v = svc.tasks_version()
    svc.clear_lifecycle(task.id)
    assert svc.tasks_version() > v
    steady = svc.tasks_version()
    svc.clear_lifecycle(task.id)
    assert svc.tasks_version() == steady


# -- provisioning: the session service does the host git; the service only records it (ADR 0010) --


async def test_record_provisioning_records_the_refs(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.set_slug(task.id, "fix-widget")
    out = await svc.record_provisioning(
        task.id, branch="panopticon/fix-widget", clone="/clones/id1"
    )
    assert (out.branch, out.clone) == ("panopticon/fix-widget", "/clones/id1")
    reloaded = await svc.get_task(task.id)  # a pure recorded-fact write; it persisted
    assert (reloaded.branch, reloaded.clone) == ("panopticon/fix-widget", "/clones/id1")


async def test_record_provisioning_is_slug_gated(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")  # no slug yet — the branch is named from the slug
    with pytest.raises(ValueError, match="slug"):
        await svc.record_provisioning(task.id, branch="panopticon/x", clone="/clones/x")
    assert (await svc.get_task(task.id)).branch is None


async def test_illegal_transition_rejected(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    with pytest.raises(IllegalTransition):
        await svc.request_transition(task.id, "WORK")  # not a free-form state


async def test_set_slug(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.set_slug(task.id, "fix-widget")
    assert (await svc.get_task(task.id)).slug == "fix-widget"


async def test_set_slug_aliases_the_artifacts_dir(tmp_path: Path) -> None:
    # Setting the slug exposes the task's artifacts under <root>/tasks/<slug> (the symlink).
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.put_artifact(task.id, "plan.md", b"# Plan\n")
    await svc.set_slug(task.id, "fix-widget")
    alias = tmp_path / "tasks" / "fix-widget"
    assert alias.is_symlink()
    assert (alias / "plan.md").read_bytes() == b"# Plan\n"


async def test_re_slug_swaps_the_alias(tmp_path: Path) -> None:
    # Re-slugging drops the stale alias and points a fresh one at the same task.
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.put_artifact(task.id, "plan.md", b"# Plan\n")
    await svc.set_slug(task.id, "old-name")
    await svc.set_slug(task.id, "new-name")
    assert not (tmp_path / "tasks" / "old-name").exists()
    assert (tmp_path / "tasks" / "new-name" / "plan.md").read_bytes() == b"# Plan\n"


# -- artifacts ----------------------------------------------------------------------


async def test_artifacts_require_task(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.put_artifact("ghost", "plan.md", b"x")


async def test_artifact_roundtrip(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.put_artifact(task.id, "plan.md", b"# Plan")
    assert await svc.get_artifact(task.id, "plan.md") == b"# Plan"
    assert await svc.list_artifacts(task.id) == ["plan.md"]


async def test_briefing_surfaces_the_plan_uri_once_the_plan_exists(tmp_path: Path) -> None:
    # The briefing names the plan's canonical URI only after the plan.md artifact is written, so the
    # agent reads it back at the right URI instead of guessing.
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "github-peer-reviewed")
    assert "panopticon://" not in await svc.briefing(task.id)  # no plan yet → no URI
    await svc.put_artifact(task.id, "plan.md", b"# Plan")
    assert f"panopticon://tasks/{task.id}/artifacts/plan.md" in await svc.briefing(task.id)


# -- liveness -----------------------------------------------------------------------


async def test_register_deregister(tmp_path: Path) -> None:
    # Liveness is connection-scoped: a registration lives exactly as long as the container holds
    # its `/live` connection. register == connect; deregister == disconnect. No heartbeat, no TTL.
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    reg = await svc.register(task.id, container_id="c-abc", runner_id="runner-1")
    assert reg.task_id == task.id
    assert [r.id for r in svc.registrations(task.id)] == [reg.id]

    await svc.deregister(reg.id)
    assert svc.registrations(task.id) == []


async def test_register_requires_task(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.register("ghost", container_id="c-abc")


async def test_registrations_do_not_age_out_on_the_clock(tmp_path: Path) -> None:
    # The old model reaped registrations on a wall-clock TTL; the connection model never does — a
    # registration is removed only by an explicit deregister (the dropped connection), so reading
    # `registrations` touches no clock no matter how much time passes.
    now = {"t": "2026-01-01T00:00:00+00:00"}
    svc = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path),
        clock=lambda: now["t"],
    )
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    task = await svc.create_task("r1", "spike")
    reg = await svc.register(task.id, container_id="c-abc")

    now["t"] = "2026-01-01T01:00:00+00:00"  # +1h — would be long past any old TTL
    assert [r.id for r in svc.registrations(task.id)] == [
        reg.id
    ]  # still live: only disconnect reaps


# -- responsibilities ---------------------------------------------------------------


class _Gated(Workflow):
    name = "gated"

    class Working(InitialState):
        label = "WORKING"
        responsibilities = (Responsibility(key="tests-pass", description="Tests pass"),)
        transitions = (Complete,)

    initial = Working


async def make_gated_service(tmp_path: Path) -> TaskService:
    svc = TaskService(
        SqlAlchemyStore(),
        {"gated": _Gated()},
        FilesystemArtifactStore(tmp_path),
    )
    await svc.init()
    await svc.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git"))
    return svc


async def test_resolve_responsibility_unblocks_transition(tmp_path: Path) -> None:
    svc = await make_gated_service(tmp_path)
    task = await svc.create_task("r1", "gated")  # starts in WORKING, promise PENDING
    with pytest.raises(ResponsibilitiesNotMet):
        await svc.request_transition(task.id, "COMPLETE")
    await svc.resolve_responsibility(task.id, "tests-pass", status=Status.MET)
    done = await svc.request_transition(task.id, "COMPLETE")
    assert done.state == "COMPLETE"


async def test_report_unknown_responsibility_rejected(tmp_path: Path) -> None:
    svc = await make_gated_service(tmp_path)
    task = await svc.create_task("r1", "gated")
    with pytest.raises(ValueError):
        await svc.resolve_responsibility(task.id, "ghost", status=Status.MET)


# -- free state override (the user can move freely) + free operations ----------------


async def test_set_state_is_a_free_move_off_graph_and_ungated(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "github-peer-reviewed")  # PLANNING, plan-written unmet
    # Skip straight to MERGING — not a legal transition, and the gate is unmet — yet it succeeds.
    moved = await svc.set_state(task.id, "MERGING")
    assert moved.state == "MERGING"
    assert (await svc.get_task(task.id)).history[-1].trigger == "set-state"


async def test_set_state_can_reopen_a_terminal_task(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "spike")
    await svc.request_transition(task.id, "COMPLETE")  # terminal
    await svc.set_state(task.id, "ITERATING")  # the user can move even out of a terminal
    assert (await svc.get_task(task.id)).state == "ITERATING"


async def test_workflow_states_lists_every_state(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "github-peer-reviewed")
    assert set(await svc.workflow_states(task.id)) == {
        "PLANNING",
        "ITERATING",
        "REVIEW",
        "MERGING",
        "COMPLETE",
        "DROPPED",
    }


async def test_going_back_to_coding_uses_set_state_not_an_operation(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)
    task = await svc.create_task("r1", "github-peer-reviewed")
    await svc.set_state(task.id, "REVIEW")  # jump to REVIEW (pr-reviewed now PENDING)
    assert "iterate" not in await svc.operations(task.id)  # no such operation
    await svc.set_state(task.id, "ITERATING")  # free move back to coding, despite the unmet promise
    assert (await svc.get_task(task.id)).state == "ITERATING"


# -- repo env_file (secrets-file) existence validation, ADR 0007 / #291 ----------------------------
#
# env_file is a *name* under the secrets dir ($PANOPTICON_CONFIG/secrets); each runner resolves it
# against its own host's secrets dir at spawn. The task service validates existence against this
# host's secrets dir on create/update, so these tests point $PANOPTICON_CONFIG at a tmp dir.


def _make_secret(config_dir: Path, name: str) -> None:
    """Create a secrets file ``name`` under ``config_dir/secrets`` (the resolved env_file target)."""
    secrets = config_dir / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / name).write_text("ANTHROPIC_API_KEY=sk-test\n")


async def test_create_repo_accepts_no_env_file(tmp_path: Path) -> None:
    svc = await make_service(tmp_path)  # r1 (no env_file) is created here — the None case
    await svc.create_repo(
        Repo(id="r2", name="acme/other", git_url="https://x/r2.git", env_file=None)
    )
    assert (await svc.get_repo("r2")).env_file is None


async def test_create_repo_accepts_an_existing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    _make_secret(tmp_path, "r2.env")
    svc = await make_service(tmp_path)
    await svc.create_repo(
        Repo(id="r2", name="acme/other", git_url="https://x/r2.git", env_file="r2.env")
    )
    assert (await svc.get_repo("r2")).env_file == "r2.env"


async def test_create_repo_rejects_a_missing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    svc = await make_service(tmp_path)
    with pytest.raises(ValueError, match="env_file"):
        await svc.create_repo(
            Repo(id="r2", name="acme/other", git_url="https://x/r2.git", env_file="absent.env")
        )
    with pytest.raises(NotFound):  # the rejected repo was not persisted
        await svc.get_repo("r2")


async def test_create_repo_rejects_an_env_file_that_is_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    (tmp_path / "secrets" / "adir").mkdir(parents=True)  # a name that resolves to a dir, not a file
    svc = await make_service(tmp_path)
    with pytest.raises(ValueError, match="env_file"):  # isfile, not merely exists
        await svc.create_repo(
            Repo(id="r2", name="acme/other", git_url="https://x/r2.git", env_file="adir")
        )


async def test_create_repo_rejects_an_env_file_that_escapes_the_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    svc = await make_service(tmp_path)
    with pytest.raises(ValueError, match="escapes"):  # secrets_file_path guards against ../ names
        await svc.create_repo(
            Repo(id="r2", name="acme/other", git_url="https://x/r2.git", env_file="../escape.env")
        )


async def test_update_repo_rejects_setting_a_missing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    svc = await make_service(tmp_path)
    with pytest.raises(ValueError, match="env_file"):
        await svc.update_repo("r1", {"env_file": "absent.env"})
    assert (await svc.get_repo("r1")).env_file is None  # unchanged


async def test_update_repo_accepts_setting_an_existing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    _make_secret(tmp_path, "r1.env")
    svc = await make_service(tmp_path)
    updated = await svc.update_repo("r1", {"env_file": "r1.env"})
    assert updated.env_file == "r1.env"


async def test_update_repo_not_touching_env_file_skips_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A repo whose env_file was valid at create can have the file vanish later; an unrelated patch
    # (here toggling workflows) must not fail on it — env_file is validated only when it's in the change set.
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    _make_secret(tmp_path, "r1.env")
    svc = await make_service(tmp_path)
    await svc.update_repo("r1", {"env_file": "r1.env"})
    (tmp_path / "secrets" / "r1.env").unlink()  # file goes away out of band
    updated = await svc.update_repo(
        "r1", {"enabled_workflows": ["spike"]}
    )  # no env_file in the patch
    assert updated.enabled_workflows == ["spike"]
    assert updated.env_file == "r1.env"  # preserved, not re-validated
