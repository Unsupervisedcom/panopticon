"""Golden tests for the Workflow state machine — the durable behavioral contract.

Pins state-class discovery, string/class transition resolution, the inherited DROPPED,
turn-on-enter, the advanced-by policy, the responsibility-promise model (seeded PENDING on
entry, fulfilled one at a time, gating the next advance), and the drop escape hatch, so later
slices can refactor freely while proving behavior is preserved.
"""

from __future__ import annotations

import dataclasses

import pytest

from panopticon.core import (
    Actor,
    Complete,
    IllegalTransition,
    InitialState,
    InvalidWorkflow,
    ResponsibilitiesNotMet,
    Responsibility,
    State,
    Status,
    Workflow,
)


class GatedWorkflow(Workflow):
    """PLAN (user approves → leave) -> WORKING (agent, gated) -> COMPLETE; DROPPED inherited."""

    name = "gated-test"

    class Plan(InitialState):
        label = "PLAN"  # initial: turn_on_enter=USER (waits on the user), advanced_by=USER (user approves to leave)
        transitions = ("WORKING",)  # forward reference resolved by label

    class Working(State):
        label = "WORKING"
        advanced_by = Actor.AGENT  # the agent advances itself once gated responsibilities are met
        responsibilities = (
            Responsibility(key="tests-pass", description="Tests pass"),
            Responsibility(key="pr-opened", description="PR opened"),
        )
        transitions = (Complete,)

    initial = Plan


WF = GatedWorkflow()


def _to_working() -> object:
    """A task advanced into WORKING — whose two responsibilities are now promised, PENDING."""
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "WORKING", at="t1")  # PLAN is ungated
    return task


# -- start_task ---------------------------------------------------------------------


def test_start_task_sets_initial_state_turn_and_history() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    assert task.state == "PLAN"
    assert task.turn is Actor.USER  # PLAN is the initial state → turn starts with the user
    assert task.slug is None
    assert task.initial_prompt is None
    assert task.history[0].from_state is None
    assert task.history[0].to_state == "PLAN"
    assert task.history[0].trigger == "start"
    assert task.history[0].responsibilities == []  # PLAN is ungated


def test_start_task_records_initial_prompt() -> None:
    task = WF.start_task("t1", "r1", at="t0", initial_prompt="review your plan")
    assert task.initial_prompt == "review your plan"


def test_start_task_seeds_starting_model_from_default_model() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    assert task.starting_model == WF.default_model


def test_workflow_default_model_is_opus() -> None:
    assert WF.default_model == "opus"


# -- resolution: string + class refs, inherited DROPPED -----------------------------


def test_transitions_resolve_strings_classes_and_inherited_drop() -> None:
    assert set(WF.transitions("PLAN")) == {"WORKING", "DROPPED"}  # string ref + inherited
    assert set(WF.transitions("WORKING")) == {"COMPLETE", "DROPPED"}  # class ref + inherited
    assert list(WF.transitions("COMPLETE")) == []  # terminal


def test_can_transition_and_terminals() -> None:
    assert WF.can_transition("PLAN", "WORKING")
    assert not WF.can_transition("PLAN", "COMPLETE")  # not a direct edge
    assert WF.is_terminal("COMPLETE")
    assert WF.is_terminal("DROPPED")
    assert not WF.is_terminal("PLAN")


def test_labels_lists_states_then_builtin_terminals() -> None:
    assert list(WF.labels()) == ["PLAN", "WORKING", "COMPLETE", "DROPPED"]


# -- turn_on_enter and advanced_by (orthogonal, declared per state) -----------------


def test_turn_on_enter_is_declared_not_derived() -> None:
    assert WF.turn_on_enter("PLAN") is Actor.USER  # initial state → user holds the turn
    assert WF.turn_on_enter("WORKING") is Actor.AGENT
    assert WF.turn_on_enter("COMPLETE") is Actor.USER  # terminal: turn returns to the user
    assert WF.turn_on_enter("DROPPED") is Actor.USER


def test_advanced_by_policy() -> None:
    assert WF.advanced_by("PLAN") is Actor.USER  # default: user approves to leave
    assert WF.advanced_by("WORKING") is Actor.AGENT  # overridden: agent advances when satisfied
    with pytest.raises(InvalidWorkflow):
        WF.advanced_by("COMPLETE")  # terminal states do not advance


def test_turn_updates_on_each_transition() -> None:
    task = _to_working()
    assert task.turn is Actor.AGENT  # WORKING.turn_on_enter
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    task.resolve_responsibility(key="pr-opened", status=Status.MET)
    WF.apply_transition(task, "COMPLETE", at="t2")
    assert task.turn is Actor.USER  # COMPLETE is terminal → back to the user


# -- named core operations (advance / drop, derived; declared ops) ------------------


def test_operations_derive_advance_and_imply_drop() -> None:
    # PLAN and WORKING each have a single non-DROPPED edge → advance is derived; drop is implicit.
    assert WF.operations("PLAN") == {"advance": "WORKING", "drop": "DROPPED"}
    assert WF.operations("WORKING") == {"advance": "COMPLETE", "drop": "DROPPED"}
    assert WF.operations("COMPLETE") == {}  # terminal: no operations


def test_resolve_operation_returns_target_or_raises() -> None:
    assert WF.resolve_operation("PLAN", "advance") == "WORKING"
    assert WF.resolve_operation("WORKING", "drop") == "DROPPED"
    with pytest.raises(IllegalTransition):
        WF.resolve_operation("PLAN", "iterate")  # not offered here


def test_declared_operation_must_target_a_legal_transition() -> None:
    # Operations are named verbs for the declared, gated graph; off-graph moves are free moves
    # (set_state), not operations. So a declared op whose target isn't a transition is rejected.
    class Bad(Workflow):
        name = "bad-op"

        class A(State):
            label = "A"
            transitions = ("B",)
            operations = {"jump": "A"}  # self-target is not a transition

        class B(State):
            label = "B"
            transitions = (Complete,)

        initial = A

    with pytest.raises(InvalidWorkflow):
        Bad().operations("A")


def test_force_transition_is_a_free_ungated_move() -> None:
    task = _to_working()  # WORKING has unresolved promises and only COMPLETE/DROPPED edges
    WF.force_transition(task, "PLAN", at="t2", trigger="set-state")  # backward, ungated, off-graph
    assert task.state == "PLAN"
    assert task.turn is Actor.USER  # PLAN.turn_on_enter (initial state → user)
    assert task.history[-1].from_state == "WORKING"
    with pytest.raises(InvalidWorkflow):
        WF.force_transition(task, "GHOST", at="t3")  # target must still exist


# -- illegal transitions ------------------------------------------------------------


def test_undefined_transition_is_rejected() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "COMPLETE", at="t1")  # PLAN -> COMPLETE not an edge


def test_cannot_transition_out_of_terminal() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "DROPPED", at="t1")
    with pytest.raises(IllegalTransition):
        WF.apply_transition(task, "WORKING", at="t2")


# -- responsibilities: promised on entry, fulfilled one at a time -------------------


def test_entering_gated_state_seeds_pending_promises() -> None:
    task = _to_working()
    entry = task.history[-1]  # the entry recorded on entering WORKING
    assert entry.to_state == "WORKING"
    assert {r.key: r.status for r in entry.responsibilities} == {
        "tests-pass": Status.PENDING,
        "pr-opened": Status.PENDING,
    }
    assert {r.description for r in entry.responsibilities} == {"Tests pass", "PR opened"}


def test_leaving_gated_state_requires_all_resolved() -> None:
    task = _to_working()
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "COMPLETE", at="t2")  # nothing resolved
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    with pytest.raises(ResponsibilitiesNotMet):
        WF.apply_transition(task, "COMPLETE", at="t2")  # pr-opened still PENDING


def test_all_met_allows_transition_and_keeps_the_record() -> None:
    task = _to_working()
    working_entry = task.history[-1]
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    task.resolve_responsibility(key="pr-opened", status=Status.MET)
    WF.apply_transition(task, "COMPLETE", at="t2")
    assert task.state == "COMPLETE"
    # the resolved promises stay on the WORKING entry that owned them
    assert {r.key: r.status for r in working_entry.responsibilities} == {
        "tests-pass": Status.MET,
        "pr-opened": Status.MET,
    }
    assert task.history[-1].responsibilities == []  # COMPLETE defines none


def test_failed_with_comment_allows_transition() -> None:
    task = _to_working()
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    task.resolve_responsibility(key="pr-opened", status=Status.FAILED, comment="forge down")
    WF.apply_transition(task, "COMPLETE", at="t2")
    assert task.state == "COMPLETE"


def test_drop_bypasses_responsibilities() -> None:
    task = _to_working()  # WORKING has unresolved promises
    WF.apply_transition(task, "DROPPED", at="t2")  # always allowed
    assert task.state == "DROPPED"
    assert task.history[-1].responsibilities == []  # DROPPED defines none


def test_ungated_state_needs_no_responsibilities() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "WORKING", at="t1")  # PLAN has no responsibilities
    assert task.state == "WORKING"


# -- history ------------------------------------------------------------------------


def test_history_accumulates_in_order() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.apply_transition(task, "WORKING", at="t1", trigger="advance")
    task.resolve_responsibility(key="tests-pass", status=Status.MET)
    task.resolve_responsibility(key="pr-opened", status=Status.MET)
    WF.apply_transition(task, "COMPLETE", at="t2", trigger="finish")
    assert [(h.from_state, h.to_state) for h in task.history] == [
        (None, "PLAN"),
        ("PLAN", "WORKING"),
        ("WORKING", "COMPLETE"),
    ]
    assert task.history[1].trigger == "advance"


def test_history_entry_transition_facts_are_frozen() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.history[0].to_state = "WORKING"  # type: ignore[misc]


# -- provisioning seam --------------------------------------------------------------


def test_provision_defaults_to_noop_and_is_overridable() -> None:
    task = WF.start_task("t1", "r1", at="t0")
    WF.provision(task, branch="panopticon/x", worktree_path="/wt/x")  # base no-op: does nothing

    calls: list[tuple[str, str]] = []

    class Provisioning(GatedWorkflow):
        name = "provisioning"

        def provision(self, task: object, *, branch: str, worktree_path: str) -> None:  # type: ignore[override]
            calls.append((branch, worktree_path))

    Provisioning().provision(task, branch="panopticon/x", worktree_path="/wt/x")
    assert calls == [("panopticon/x", "/wt/x")]


# -- workflow validation (lazy: on first use, then cached) --------------------------


def test_validate_rejects_unknown_initial() -> None:
    class Bad(Workflow):
        name = "bad-initial"

        class A(State):
            label = "A"
            transitions = (Complete,)

        initial = "NOPE"

    with pytest.raises(InvalidWorkflow):
        Bad().initial_label


def test_validate_rejects_unknown_transition_target() -> None:
    class Bad(Workflow):
        name = "bad-target"

        class A(State):
            label = "A"
            transitions = ("GHOST",)  # no such state

        initial = A

    with pytest.raises(InvalidWorkflow):
        list(Bad().labels())


def test_validate_rejects_duplicate_labels() -> None:
    class Bad(Workflow):
        name = "dupe"

        class A(State):
            label = "X"
            transitions = (Complete,)

        class B(State):
            label = "X"  # same label as A
            transitions = (Complete,)

        initial = A

    with pytest.raises(InvalidWorkflow):
        list(Bad().labels())


def test_validate_requires_initial_state_to_subclass_initialstate() -> None:
    # A freshly created task starts on the user's turn — enforced by requiring the initial state
    # to be an InitialState (turn_on_enter=USER). A plain State as initial is rejected.
    class Bad(Workflow):
        name = "bad-initial-base"

        class A(State):
            label = "A"
            transitions = (Complete,)

        initial = A

    with pytest.raises(InvalidWorkflow, match="InitialState"):
        Bad().initial_label


def test_initial_state_starts_on_the_users_turn() -> None:
    class Good(Workflow):
        name = "good-initial-base"

        class A(InitialState):
            label = "A"
            transitions = (Complete,)

        initial = A

    assert Good().turn_on_enter("A") is Actor.USER
    assert Good().start_task("t1", "r1", at="t0").turn is Actor.USER


# -- opt_in class var ---------------------------------------------------------------


def test_opt_in_defaults_to_false() -> None:
    assert GatedWorkflow.opt_in is False


def test_opt_in_can_be_overridden_to_true() -> None:
    class OptInWorkflow(GatedWorkflow):
        name = "opt-in-wf"
        opt_in = True

    assert OptInWorkflow.opt_in is True
    assert OptInWorkflow().opt_in is True


def test_opt_in_does_not_bleed_across_subclasses() -> None:
    class A(GatedWorkflow):
        name = "opt-in-a"
        opt_in = True

    class B(GatedWorkflow):
        name = "opt-in-b"

    assert A.opt_in is True
    assert B.opt_in is False
