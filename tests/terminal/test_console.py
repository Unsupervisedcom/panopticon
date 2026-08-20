"""The terminal session supervisor (ADR 0009 §6).

The dashboard step (`show_dashboard`) and the tmux attach (`attach`) are injected, so the
hub-and-spoke loop is tested without a TTY or tmux; `switch_to`'s detach is injected too.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from panopticon.terminal.console import (
    REVIEW_HEAD_SHA_VAR,
    ReviewResult,
    ReviewTarget,
    list_review_sessions,
    make_review_sessions_probe,
    make_review_switch,
    reap_orphan_review_sessions,
    resolve_join,
    review_session_name,
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


# -- `v`: open tarot on a task's work (make_review_switch) ---------------------------
#
# Same injected-fake style as the `t`/`s`/`u` hooks above: `run` captures the emitted tmux
# `new-session` argv, `exists`/`tarot_installed`/`configured_base`/`clone_present`/`detach` stub
# the host, so the fallback ladder + the exact command are pinned without tmux, git, or tarot.


def _review(switch: Path, launched: list[list[str]], detached: list[bool], **overrides: Any):
    """A make_review_switch with all host interactions faked; `overrides` tweak per-test."""
    kwargs: dict[str, Any] = {
        "service_url": "http://svc:8000",
        "exists": lambda _s: False,
        "tarot_installed": lambda: True,
        "configured_base": lambda _clone: None,
        "clone_present": lambda _p: True,
        "head_sha": lambda _clone: None,  # hermetic: no real git (staleness tests override)
        "stored_sha": lambda _session: None,  # hermetic: no real tmux show-environment
        "run": lambda argv: launched.append(argv),
        "detach": lambda: detached.append(True),
    }
    kwargs.update(overrides)
    return make_review_switch(switch, **kwargs)


def test_review_launches_tarot_on_the_local_clone(tmp_path: Path) -> None:
    # A local task (no runner_host) with a present clone and no `tarot.base` config: create the
    # detached review session in the clone with `--base origin/<default_base>` and the ask-the-author
    # env, then record the pick + detach (the same hand-off `t` uses).
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached)

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.LAUNCHED
    assert launched == [
        [
            "tmux", "-L", "panopticon", "new-session", "-d", "-s", "panopticon-review-t1",
            "-c", "/clones/t1",
            "-e", "PANOPTICON_SERVICE_URL=http://svc:8000",
            "-e", "TAROT_PANOPTICON_TASK=t1",
            "tarot", "--base", "origin/main",
        ]
    ]  # fmt: skip
    assert switch.read_text() == "panopticon-review-t1"  # recorded for the supervisor to attach
    assert detached == [True]


def test_review_respects_a_clone_tarot_base_config(tmp_path: Path) -> None:
    # When the clone has a `tarot.base` git config, don't force `--base` — let tarot read its config.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, configured_base=lambda _clone: "origin/develop")

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.LAUNCHED
    assert "--base" not in launched[0]  # config wins — we pass no base
    assert launched[0][-1] == "tarot"


def test_review_falls_back_to_the_url_when_the_clone_is_missing(tmp_path: Path) -> None:
    # No local clone on this host (reaped) but a PR url exists: `tarot <url>`, no `-c`, no `--base`.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, clone_present=lambda _p: False)

    result = review(
        ReviewTarget(
            task_id="t1",
            clone="/clones/t1",
            url="https://forge/pr/1",
            runner_host=None,
            default_base="main",
        )
    )

    assert result is ReviewResult.LAUNCHED
    assert launched == [
        [
            "tmux", "-L", "panopticon", "new-session", "-d", "-s", "panopticon-review-t1",
            "-e", "PANOPTICON_SERVICE_URL=http://svc:8000",
            "-e", "TAROT_PANOPTICON_TASK=t1",
            "tarot", "https://forge/pr/1",
        ]
    ]  # fmt: skip
    assert "-c" not in launched[0]
    assert switch.read_text() == "panopticon-review-t1"
    assert detached == [True]


def test_review_on_a_remote_runner_skips_the_local_clone(tmp_path: Path) -> None:
    # The clone lives on the runner host, not here — even with a clone path recorded, a set
    # runner_host means local-clone review is skipped and we fall back to the url.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    # clone_present would say True, but runner_host must veto the local path.
    review = _review(switch, launched, detached, clone_present=lambda _p: True)

    result = review(
        ReviewTarget(
            task_id="t1",
            clone="/clones/t1",
            url="https://forge/pr/1",
            runner_host="box.example.com",
            default_base="main",
        )
    )

    assert result is ReviewResult.LAUNCHED
    assert launched[0][-2:] == ["tarot", "https://forge/pr/1"]  # url path, not the clone
    assert "-c" not in launched[0]


def test_review_reports_nothing_to_review_with_no_clone_and_no_url(tmp_path: Path) -> None:
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, clone_present=lambda _p: False)

    result = review(
        ReviewTarget(task_id="t1", clone=None, url=None, runner_host=None, default_base="main")
    )

    assert result is ReviewResult.NOTHING_TO_REVIEW
    assert launched == [] and detached == []  # nothing spawned, no hand-off
    assert not switch.exists()  # no pick recorded


def test_review_reports_no_tarot_when_not_installed(tmp_path: Path) -> None:
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, tarot_installed=lambda: False)

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.NO_TAROT
    assert launched == [] and detached == []  # never spawn a session that would instantly die
    assert not switch.exists()  # no pick recorded


def test_review_reattaches_when_a_session_already_exists(tmp_path: Path) -> None:
    # Second press while a review session is up: re-attach (record + detach), never double-launch.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, exists=lambda _s: True)

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.REATTACHED
    assert launched == []  # no new-session
    assert switch.read_text() == "panopticon-review-t1" and detached == [True]  # re-attaches


def test_review_records_the_launch_head_sha_for_staleness(tmp_path: Path) -> None:
    # A local-clone launch stamps the clone's HEAD into the session env, so a later `v` can tell
    # whether the clone advanced.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(switch, launched, detached, head_sha=lambda _c: "sha-old")

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.LAUNCHED
    assert "-e" in launched[0] and f"{REVIEW_HEAD_SHA_VAR}=sha-old" in launched[0]


def test_reattach_is_instant_when_the_clone_has_not_advanced(tmp_path: Path) -> None:
    # Existing session + local clone whose HEAD equals the launch sha: a pure re-attach — no
    # set-environment, no respawn.
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(
        switch,
        launched,
        detached,
        exists=lambda _s: True,
        head_sha=lambda _c: "sha-same",
        stored_sha=lambda _s: "sha-same",
    )

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.REATTACHED
    assert launched == []  # nothing respawned
    assert switch.read_text() == "panopticon-review-t1" and detached == [True]


def test_reattach_restarts_tarot_when_the_clone_advanced(tmp_path: Path) -> None:
    # Existing session + local clone whose HEAD advanced past the launch sha: update the recorded
    # sha (set-environment) and restart tarot in place (respawn-window -k), env preserved, then
    # attach — RELOADED so the dashboard can say "PR advanced".
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(
        switch,
        launched,
        detached,
        exists=lambda _s: True,
        head_sha=lambda _c: "sha-new",
        stored_sha=lambda _s: "sha-old",
    )

    result = review(
        ReviewTarget(
            task_id="t1", clone="/clones/t1", url=None, runner_host=None, default_base="main"
        )
    )

    assert result is ReviewResult.RELOADED
    assert launched == [
        ["tmux", "-L", "panopticon", "set-environment", "-t", "panopticon-review-t1",
         REVIEW_HEAD_SHA_VAR, "sha-new"],
        ["tmux", "-L", "panopticon", "respawn-window", "-k", "-t", "panopticon-review-t1",
         "-c", "/clones/t1", "tarot", "--base", "origin/main"],
    ]  # fmt: skip
    assert switch.read_text() == "panopticon-review-t1" and detached == [True]


def test_reattach_to_a_url_review_never_checks_staleness(tmp_path: Path) -> None:
    # A url/remote review has no on-disk HEAD to diff — a second press is always a pure re-attach,
    # even though head_sha would report a change (it must not be consulted).
    switch = tmp_path / "switch"
    launched: list[list[str]] = []
    detached: list[bool] = []
    review = _review(
        switch,
        launched,
        detached,
        exists=lambda _s: True,
        clone_present=lambda _p: False,
        head_sha=lambda _c: "sha-new",
        stored_sha=lambda _s: "sha-old",
    )

    result = review(
        ReviewTarget(
            task_id="t1",
            clone="/clones/t1",
            url="https://forge/pr/1",
            runner_host=None,
            default_base="main",
        )
    )

    assert result is ReviewResult.REATTACHED
    assert launched == []  # no respawn for the url case


# -- warm-review sessions: the dashboard marker + orphan reaping ----------------------


def _sessions_run(names: list[str]) -> Any:
    """A fake `run` for list_review_sessions: returns an object with a `stdout` of tmux
    `list-sessions -F '#{session_name}'` output (one name per line)."""

    class _Result:
        stdout = "\n".join(names) + ("\n" if names else "")

    return lambda _argv: _Result()


def test_list_review_sessions_extracts_task_ids() -> None:
    # Only `panopticon-review-<id>` sessions are review sessions; the dashboard/task sessions and
    # any other name are ignored.
    warm = list_review_sessions(
        run=_sessions_run(
            ["panopticon-review-a", "panopticon-b", "dashboard", "panopticon-review-c"]
        )
    )
    assert warm == {"a", "c"}


def test_list_review_sessions_is_empty_on_tmux_error() -> None:
    # No server / a raising tmux → empty set (a refresh must never crash on this).
    def _boom(_argv: list[str]) -> Any:
        raise OSError("no server running")

    assert list_review_sessions(run=_boom) == set()


def test_reap_orphan_review_sessions_kills_only_gone_tasks() -> None:
    # A review session whose task is absent from the live set is killed; a still-listed task's
    # session is kept (even terminal ones stay reviewable).
    killed: list[list[str]] = []
    reaped = reap_orphan_review_sessions(
        {"present"},
        sessions={"present", "gone"},
        run=lambda argv: killed.append(argv),
    )
    assert reaped == ["gone"]
    assert killed == [["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-review-gone"]]


def test_review_sessions_probe_marks_survivors_and_reaps_orphans(monkeypatch: Any) -> None:
    # The dashboard's per-tick probe: list once, reap sessions of gone tasks, return the warm set
    # intersected with the live ids (what the row marker uses).
    killed: list[list[str]] = []
    monkeypatch.setattr(
        "panopticon.terminal.console.list_review_sessions",
        lambda **_kw: {"live1", "gone"},
    )
    monkeypatch.setattr(
        "panopticon.terminal.console.subprocess.run",
        lambda argv, **_kw: killed.append(argv),
    )
    probe = make_review_sessions_probe()

    warm = probe({"live1", "live2"})

    assert warm == {"live1"}  # gone reaped out; live2 has no session so isn't marked
    assert killed == [["tmux", "-L", "panopticon", "kill-session", "-t", "panopticon-review-gone"]]


# -- integration: a real tarot review session on real tmux ---------------------------


@pytest.mark.skipif(
    not (shutil.which("tmux") and shutil.which("tarot") and shutil.which("git")),
    reason="needs tmux, tarot, and git",
)
def test_opens_a_real_review_session_and_returns_on_quit(tmp_path: Path) -> None:
    # End to end on a throwaway socket: a real fixture clone, real tmux + tarot. The launcher
    # creates a detached `panopticon-review-<id>` session running tarot in the clone; quitting
    # tarot (`q`) ends the session — the supervisor's cue to return to the dashboard.
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "--initial-branch", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README").write_text("hi")
    run("git", "add", "--all")
    run("git", "commit", "--message", "init")

    socket = "panopticon-review-test"
    session = review_session_name("t1")
    switch = tmp_path / "switch"
    try:
        review = make_review_switch(
            switch, service_url="http://svc:8000", socket=socket, detach=lambda: None
        )
        result = review(
            ReviewTarget(
                task_id="t1", clone=str(repo), url=None, runner_host=None, default_base="main"
            )
        )
        assert result is ReviewResult.LAUNCHED
        assert switch.read_text() == session

        def has_session() -> bool:
            return (
                subprocess.run(
                    ["tmux", "-L", socket, "has-session", "-t", session], capture_output=True
                ).returncode
                == 0
            )

        # tmux gives the detached session a pty, so the tarot TUI stays up waiting for input.
        assert any(has_session() or time.sleep(0.1) for _ in range(20))
        panes = subprocess.run(
            ["tmux", "-L", socket, "list-panes", "-t", session, "-F", "#{pane_start_command}"],
            capture_output=True,
            text=True,
        ).stdout
        assert "tarot" in panes  # the session is actually running tarot

        subprocess.run(["tmux", "-L", socket, "send-keys", "-t", session, "q"], capture_output=True)
        # quitting tarot ends the session — the loop returns to the dashboard.
        assert any((not has_session()) or time.sleep(0.1) for _ in range(30))
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
