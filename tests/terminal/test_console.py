"""The terminal session supervisor (ADR 0009 §6).

The dashboard step (`show_dashboard`) and the tmux attach (`attach`) are injected, so the
hub-and-spoke loop is tested without a TTY or tmux; `switch_to`'s detach is injected too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panopticon.terminal.console import (
    resolve_join,
    run_console,
    switch_file_path,
    switch_to,
    wait_for_service,
)


class _JoinClient:
    """A fake task-service client for resolve_join: canned tasks + per-task registrations."""

    def __init__(
        self, tasks: list[dict[str, Any]], registrations: dict[str, list[dict[str, Any]]]
    ) -> None:
        self._tasks = tasks
        self._registrations = registrations

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._tasks

    def list_registrations(self, task_id: str) -> list[dict[str, Any]]:
        return self._registrations.get(task_id, [])


def test_switch_file_is_deterministic_per_socket() -> None:
    # The dashboard session outlives the supervisor, so the switch-file must be stable across
    # `make start` re-invocations — otherwise a re-attached dashboard writes its `t` pick to a
    # file the new supervisor isn't reading, and every `t` reads as a quit (operator dropped to shell).
    assert switch_file_path("panopticon") == switch_file_path(
        "panopticon"
    )  # same socket → same path
    assert switch_file_path("panopticon") != switch_file_path("other")  # keyed by socket


def test_wait_for_service_polls_until_ready() -> None:
    # Gates the dashboard on the service being up (the `make start` startup race): poll until
    # the health check passes, then proceed.
    calls = {"n": 0}

    def ready(_url: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3  # up on the third poll

    assert wait_for_service("http://svc", ready=ready, sleep=lambda _s: None, attempts=10) is True
    assert calls["n"] == 3


def test_wait_for_service_gives_up_after_attempts() -> None:
    polled: list[bool] = []
    ok = wait_for_service(
        "http://svc",
        ready=lambda _u: polled.append(True) or False,
        sleep=lambda _s: None,
        attempts=5,
    )
    assert ok is False and len(polled) == 5  # bounded; reports failure rather than blocking forever


def test_loop_attaches_each_picked_session_then_stops_on_quit() -> None:
    # The supervisor shows the dashboard, attaches to each picked session, and re-shows the same
    # dashboard on detach — until the dashboard returns None (quit).
    picks = iter(["sess-a", "sess-b", None])
    attached: list[str] = []

    run_console(show_dashboard=lambda: next(picks), attach=attached.append)

    assert attached == ["sess-a", "sess-b"]  # one attach per pick, in order; None ends the loop


def test_quitting_immediately_attaches_nothing() -> None:
    attached: list[str] = []
    run_console(show_dashboard=lambda: None, attach=attached.append)
    assert attached == []


def test_initial_join_is_attached_before_the_first_dashboard() -> None:
    # `panopticon start <task>`: the joined session is attached first, then the loop shows the
    # dashboard and attaches each pick — so the operator lands straight in the joined task.
    picks = iter(["sess-b", None])
    attached: list[str] = []

    run_console(show_dashboard=lambda: next(picks), attach=attached.append, initial="sess-a")

    assert attached == ["sess-a", "sess-b"]  # joined session first, then the picked one


def test_no_initial_join_behaves_as_before() -> None:
    picks = iter(["sess-b", None])
    attached: list[str] = []

    run_console(show_dashboard=lambda: next(picks), attach=attached.append, initial=None)

    assert attached == ["sess-b"]  # no leading attach when nothing is joined


def test_switch_target_encode_decode_round_trips() -> None:
    # The one format the `t` hook, the join, and the supervisor's attach all share.
    from panopticon.terminal.console import decode_switch_target, encode_switch_target

    assert (
        encode_switch_target("panopticon-t1", "box.example.com") == "box.example.com\tpanopticon-t1"
    )
    assert encode_switch_target("panopticon-t2", None) == "panopticon-t2"
    assert decode_switch_target(encode_switch_target("s", "h")) == ("s", "h")
    assert decode_switch_target(encode_switch_target("s", None)) == ("s", None)


def test_resolve_join_by_slug_returns_the_container_session() -> None:
    # session == container id; a local task (no runner_host) encodes as a bare "<session>".
    client = _JoinClient(
        tasks=[{"id": "t1", "slug": "fix-login", "runner_host": None}],
        registrations={"t1": [{"container_id": "panopticon-t1"}]},
    )
    assert resolve_join(client, "fix-login") == "panopticon-t1"  # type: ignore[arg-type]


def test_resolve_join_by_id_returns_the_container_session() -> None:
    client = _JoinClient(
        tasks=[{"id": "t1", "slug": "fix-login", "runner_host": None}],
        registrations={"t1": [{"container_id": "panopticon-t1"}]},
    )
    assert resolve_join(client, "t1") == "panopticon-t1"  # type: ignore[arg-type]


def test_resolve_join_encodes_a_remote_task_with_its_host() -> None:
    # A remote runner (M5): the switch-file target carries "<host>\t<session>" so the supervisor
    # ssh-wraps the attach — same encoding as the dashboard's `t` hook.
    client = _JoinClient(
        tasks=[{"id": "t1", "slug": "fix-login", "runner_host": "box.example.com"}],
        registrations={"t1": [{"container_id": "panopticon-t1"}]},
    )
    assert resolve_join(client, "t1") == "box.example.com\tpanopticon-t1"  # type: ignore[arg-type]


def test_resolve_join_returns_none_for_an_unknown_task() -> None:
    client = _JoinClient(tasks=[{"id": "t1", "slug": "fix-login"}], registrations={})
    assert resolve_join(client, "nope") is None  # type: ignore[arg-type]


def test_resolve_join_returns_none_when_no_container_is_running() -> None:
    # The task exists but has no registration (container not up) → fall back to the dashboard.
    client = _JoinClient(
        tasks=[{"id": "t1", "slug": "fix-login", "runner_host": None}], registrations={"t1": []}
    )
    assert resolve_join(client, "fix-login") is None  # type: ignore[arg-type]


def test_resolve_join_polls_across_the_reconnect_window() -> None:
    # `start` (re)starts the service, wiping in-memory registrations; the container re-registers a
    # beat later. resolve_join polls and resolves on the first hit rather than racing the reconnect.
    registrations: dict[str, list[dict[str, Any]]] = {"t1": []}
    client = _JoinClient(
        tasks=[{"id": "t1", "slug": "fix-login", "runner_host": None}], registrations=registrations
    )

    naps = {"n": 0}

    def sleep(_s: float) -> None:
        naps["n"] += 1
        if naps["n"] == 3:  # container reconnects on the 3rd poll
            registrations["t1"] = [{"container_id": "panopticon-t1"}]

    assert (
        resolve_join(client, "fix-login", attempts=25, interval=0.2, sleep=sleep)  # type: ignore[arg-type]
        == "panopticon-t1"
    )
    assert naps["n"] == 3  # stopped polling once it appeared


def test_resolve_join_does_not_poll_for_an_unknown_task() -> None:
    # A typo'd ref can't be conjured by waiting — bail immediately instead of burning the window.
    client = _JoinClient(tasks=[{"id": "t1", "slug": "fix-login"}], registrations={})
    naps = {"n": 0}
    assert (
        resolve_join(  # type: ignore[arg-type]
            client,
            "nope",
            attempts=25,
            interval=0.2,
            sleep=lambda _s: naps.__setitem__("n", naps["n"] + 1),
        )
        is None
    )
    assert naps["n"] == 0


def test_switch_to_records_the_pick_then_detaches(tmp_path: Path) -> None:
    # The dashboard's `t` hook: write the pick for the supervisor, then detach this client so the
    # supervisor regains the TTY and attaches the task. The dashboard process stays alive.
    detached: list[bool] = []
    switch = tmp_path / "switch"

    switch_to("panopticon-t1", switch_file=switch, detach=lambda: detached.append(True))

    assert switch.read_text() == "panopticon-t1"
    assert detached == [True]


def test_switch_to_with_remote_host_encodes_host_and_session(tmp_path: Path) -> None:
    # A remote runner (M5): the switch-file carries "<host>\t<session>" so the supervisor can
    # parse it and pass host= to attach_command for the ssh-wrapped attach.
    switch = tmp_path / "switch"

    switch_to("panopticon-t1", host="box.example.com", switch_file=switch, detach=lambda: None)

    assert switch.read_text() == "box.example.com\tpanopticon-t1"


def test_supervisor_parses_remote_host_from_switch_file(tmp_path: Path) -> None:
    # run_console_local's attach() closure decodes "<host>\t<session>" from the switch-file and
    # passes host= to attach_command; a plain session (no tab) means local (host=None).
    from panopticon.terminal.attach import attach_command
    from panopticon.terminal.console import decode_switch_target

    # Remote pick: "host\tsession"
    assert decode_switch_target("box.example.com\tpanopticon-t1") == (
        "panopticon-t1",
        "box.example.com",
    )
    # Local pick: plain "session"
    assert decode_switch_target("panopticon-t2") == ("panopticon-t2", None)

    # Confirm attach_command receives the host correctly
    assert attach_command("panopticon-t1", socket="panopticon", host="box.example.com") == [
        "ssh",
        "-t",
        "box.example.com",
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        "panopticon-t1",
    ]
    assert attach_command("panopticon-t2", socket="panopticon", host=None) == [
        "tmux",
        "-L",
        "panopticon",
        "attach",
        "-t",
        "panopticon-t2",
    ]


def test_make_service_switch_only_switches_when_a_service_session_exists(tmp_path: Path) -> None:
    from panopticon.terminal.console import SERVICE_SESSION, make_service_switch

    switch = tmp_path / "switch"

    # Service running → records the service session + detaches, reports True.
    switched = make_service_switch(switch, exists=lambda: True, detach=lambda: None)
    assert switched() is True
    assert switch.read_text() == SERVICE_SESSION

    # No service session → does nothing (no write, no detach), reports False.
    switch.write_text("")
    absent = make_service_switch(switch, exists=lambda: False, detach=lambda: None)
    assert absent() is False
    assert switch.read_text() == ""


def test_make_runner_switch_only_switches_when_a_runner_session_exists(tmp_path: Path) -> None:
    from panopticon.terminal.console import RUNNER_SESSION, make_runner_switch

    switch = tmp_path / "switch"

    # Runner running → records the runner session + detaches, reports True.
    switched = make_runner_switch(switch, exists=lambda: True, detach=lambda: None)
    assert switched() is True
    assert switch.read_text() == RUNNER_SESSION

    # No runner session → does nothing (no write, no detach), reports False.
    switch.write_text("")
    absent = make_runner_switch(switch, exists=lambda: False, detach=lambda: None)
    assert absent() is False
    assert switch.read_text() == ""
