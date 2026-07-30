"""Stalled-agent detection & recovery (ADR 0014) — acceptance tests against a **real** Docker
container + tmux session (skipped when either is unavailable, mirroring
``test_local_runner.py``'s ``test_spawn_and_stop_real_container_and_session``). No LLM: a tiny
POSIX-sh script stands in for `claude` (``fake-claude``), wrapped by a script standing in for
`container/agent.py` (``agent-wrapper``) so a killed "claude" leaves the container/session alive
— exactly the shape-B scenario `StallMonitor` exists to detect. Real `LocalRunner` (real `docker
exec`/`tmux capture-pane`/`tmux send-keys`) drives real `StallMonitor` detection/classification;
`TaskServiceClient`/`Spawner` are fakes (recording calls), since re-verifying the task-service
HTTP layer or the respawn image-build mechanics isn't this file's job — both are already covered
elsewhere (`test_spawner.py`, `test_host.py`, `test_api.py`). The four scenarios follow the
plan's VERIFICATION section: (a) alive-idle stall → detect → inject retry → the agent responds;
(b) claude's process killed in a live container → detected and routed to respawn; (c) a
still-running tool call → no intervention (the false-positive guard); (d) the recovery cap →
loud surface, cleared by a manual bump.

The clock `StallMonitor` uses is **injected** (advanced synthetically between ticks, exactly like
`test_stall.py`'s unit tests), so these tests don't wait out real idle windows or backoff — only
the handful of real `docker`/`tmux` round-trips cost real (sub-second) time.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from panopticon.client import JsonObj
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.stall import StallMonitor

_HAVE_DOCKER_TMUX = bool(shutil.which("docker") and shutil.which("tmux"))


def _docker_running() -> bool:
    return (
        _HAVE_DOCKER_TMUX
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(
    not _docker_running(), reason="needs a working docker daemon + tmux"
)

_IMAGE = "panopticon-stall-itest:latest"
_SOCKET = "panopticon-stall-itest"

# `fake-claude.sh` stands in for the real `claude` CLI: writes an initial transcript entry,
# prints a recognizable error banner (so `classify_pane_text` has something real to match), then
# reads lines from its stdin (i.e. what `tmux send-keys` delivers) forever. Two env vars change
# its behavior for the false-positive-guard and recovery-cap scenarios respectively.
#
# It's run as `claude-agent /usr/local/bin/fake-claude.sh` rather than executed directly —
# `claude-agent` is a **copy of the shell binary itself** (see the Dockerfile below), not a
# shebang-interpreted script. A shebang script's `comm` (what `ps -o comm` / `/proc/<pid>/comm`
# reports) is the *interpreter's* binary name (`sh`/`dash`), never the script's own filename — so
# `parse_process_snapshot`'s `"claude" in comm.lower()` match would never fire against a plain
# `#!/bin/sh` script. Running the renamed shell binary directly on the script (no shebang
# indirection) makes the exec'd file — and so `comm` — actually named `claude-agent`, matching
# real claude's own installed-binary shape (`CLAUDE_CODE_EXECPATH` points at a real binary, not a
# shebang script) far more faithfully than a raw script would.
_FAKE_CLAUDE = """
proj=/home/panopticon/.claude/projects/-workspace
mkdir -p "$proj"
echo '{"type":"assistant","message":{"model":"claude","usage":{}}}' > "$proj/session.jsonl"
if [ "$FAKE_CLAUDE_SPAWN_TOOL" = "1" ]; then
  sleep 30 &
fi
echo "API Error: 529 overloaded_error"
while IFS= read -r line; do
  if [ "$FAKE_CLAUDE_MODE" != "unresponsive" ]; then
    echo '{"type":"assistant","message":{"model":"claude","usage":{}}}' >> "$proj/session.jsonl"
    echo "ok: $line"
  fi
done
"""

# `agent-wrapper` stands in for `container/agent.py`: starts the fake claude and, unlike the real
# launcher (which stops the whole container when claude exits), lingers afterward — isolating
# `StallMonitor`'s own claude-absent detection from `agent.py`'s own on_exit contract (covered by
# `tests/container/test_agent.py`), so a killed fake claude leaves the container/tmux session
# alive: exactly the shape-B condition being tested. Its own `comm` doesn't matter (it's never
# meant to match "claude"), so it stays a plain shebang script.
_AGENT_WRAPPER = """#!/bin/sh
/usr/local/bin/claude-agent /usr/local/bin/fake-claude.sh
echo "claude process exited"
sleep 3600
"""


def _b64(script: str) -> str:
    return base64.b64encode(script.encode()).decode()


def _build_dockerfile() -> str:
    # `docker build --tag <image> -` takes the Dockerfile as the *entire* stdin (no separate build
    # context, so no COPY, and no multi-line heredoc — that's a BuildKit-specific RUN extension
    # this doesn't assume is enabled). Each script is base64-embedded in a single-line RUN instead,
    # which works with any builder.
    return f"""FROM python:3.13-slim
RUN apt-get update && apt-get install --yes --no-install-recommends procps \
 && rm --recursive --force /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home --home-dir /home/panopticon --shell /bin/sh panopticon
RUN cp /bin/sh /usr/local/bin/claude-agent
RUN echo '{_b64(_FAKE_CLAUDE)}' | base64 --decode > /usr/local/bin/fake-claude.sh
RUN echo '{_b64(_AGENT_WRAPPER)}' | base64 --decode > /usr/local/bin/agent-wrapper
RUN chmod +x /usr/local/bin/claude-agent /usr/local/bin/fake-claude.sh /usr/local/bin/agent-wrapper
ENTRYPOINT ["sleep", "3600"]
"""


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    subprocess.run(
        ["docker", "build", "--tag", _IMAGE, "-"],
        input=_build_dockerfile(),
        text=True,
        check=True,
        capture_output=True,
    )
    yield _IMAGE
    subprocess.run(["docker", "rmi", "--force", _IMAGE], capture_output=True)
    subprocess.run(["tmux", "-L", _SOCKET, "kill-server"], capture_output=True)


class _FakeClient:
    """Records reported lifecycle phases/clears; serves a docker-runner_type spec (fake-claude
    isn't a shell workflow)."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, str, str | None]] = []
        self.cleared: list[str] = []

    def workflow_execution(self, name: str) -> JsonObj:
        return {"runner_type": "docker", "script": "", "clone_repo": False, "workdir": None}

    def report_lifecycle(
        self, task_id: str, runner_id: str, phase: str, detail: str | None = None
    ) -> JsonObj:
        self.phases.append((task_id, phase, detail))
        return {"id": task_id}

    def clear_lifecycle(self, task_id: str) -> JsonObj:
        self.cleared.append(task_id)
        return {"id": task_id}


class _FakeSpawner:
    """Records `respawn` calls — the real respawn mechanics (image build, `--continue` argv) are
    already covered by `test_spawner.py`/`test_agent.py`; this file's job is to prove
    `StallMonitor` correctly *routes* to it when claude's process is gone."""

    def __init__(self) -> None:
        self.respawned: list[JsonObj] = []

    def respawn(self, task: JsonObj) -> str:
        self.respawned.append(task)
        return f"panopticon-{task['id']}"


def _task(task_id: str) -> JsonObj:
    return {
        "id": task_id,
        "state": "ITERATING",
        "claimed_by": "itest",
        "turn": "agent",
        "workflow": "spike",
    }


def _poll(predicate: object, *, timeout: float = 10.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(interval)
    return predicate()  # type: ignore[operator]  # one last try, for the assertion message


def _spawn(
    image: str, task_id: str, *, extra_env: dict[str, str] | None = None
) -> tuple[LocalRunner, str]:
    runner = LocalRunner(
        "http://unused",
        image=image,
        runner_id="itest",
        agent_command=["/usr/local/bin/agent-wrapper"],
        tmux_socket=_SOCKET,
        extra_env=extra_env,
    )
    container = runner.spawn(task_id)
    assert _poll(lambda: runner.is_running(task_id), timeout=15.0), "container never came up"
    assert _poll(lambda: runner.has_session(task_id), timeout=15.0), "tmux session never came up"
    # fake-claude's initial banner is the signal it's actually running (not just the container).
    assert _poll(lambda: "overloaded_error" in runner.pane_text(task_id), timeout=15.0), (
        "fake-claude never printed its banner"
    )
    return runner, container


def test_alive_idle_stall_is_detected_and_the_injected_retry_reaches_the_agent(
    image: str,
) -> None:
    task_id = "stall-a"
    runner, container = _spawn(image, task_id)
    try:
        clock = {"t": time.time()}
        client, spawner = _FakeClient(), _FakeSpawner()
        monitor = StallMonitor(
            client,  # type: ignore[arg-type]
            runner,
            spawner,  # type: ignore[arg-type]
            runner_id="itest",
            now=lambda: clock["t"],
            idle_seconds=60.0,
            probe_interval_seconds=1.0,
        )
        task = _task(task_id)

        clock["t"] += 120.0  # well past the idle threshold
        monitor.tick(task)  # first stale probe — establishes the pane baseline
        clock["t"] += 2.0
        monitor.tick(task)  # pane still frozen — detects + injects the retry

        assert client.phases, "expected a STALLED lifecycle report"
        _, phase, detail = client.phases[-1]
        assert phase == "stalled"
        assert detail is not None and "auto-retry 1" in detail and "overloaded_error" in detail
        assert spawner.respawned == []  # shape A — nudged in place, not respawned

        # fake-claude actually received the retry and responded — the transcript grew.
        assert _poll(lambda: "ok: try again" in runner.pane_text(task_id), timeout=10.0)
    finally:
        runner.stop(container)


def test_a_dead_claude_process_in_a_live_container_routes_to_respawn(image: str) -> None:
    task_id = "stall-b"
    runner, container = _spawn(image, task_id)
    try:
        subprocess.run(
            ["docker", "exec", "--user", "panopticon", container, "pkill", "-f", "fake-claude"],
            check=True,
        )
        assert _poll(lambda: not runner.process_snapshot(task_id).claude_present, timeout=10.0), (
            "fake-claude should be gone from the process tree"
        )
        # the container/session survive the kill — the whole point of this scenario
        assert runner.is_running(task_id) is True
        assert runner.has_session(task_id) is True

        clock = {"t": time.time()}
        client, spawner = _FakeClient(), _FakeSpawner()
        monitor = StallMonitor(
            client,  # type: ignore[arg-type]
            runner,
            spawner,  # type: ignore[arg-type]
            runner_id="itest",
            now=lambda: clock["t"],
            idle_seconds=60.0,
            probe_interval_seconds=1.0,
        )
        task = _task(task_id)
        clock["t"] += 120.0
        monitor.tick(task)  # baseline
        clock["t"] += 2.0
        monitor.tick(task)  # acts

        assert spawner.respawned == [task]
    finally:
        runner.stop(container)


def test_a_live_tool_call_suppresses_detection(image: str) -> None:
    task_id = "stall-c"
    runner, container = _spawn(image, task_id, extra_env={"FAKE_CLAUDE_SPAWN_TOOL": "1"})
    try:
        assert _poll(lambda: runner.process_snapshot(task_id).tool_active, timeout=10.0), (
            "the backgrounded `sleep` should show up as a live child of fake-claude"
        )

        clock = {"t": time.time()}
        client, spawner = _FakeClient(), _FakeSpawner()
        monitor = StallMonitor(
            client,  # type: ignore[arg-type]
            runner,
            spawner,  # type: ignore[arg-type]
            runner_id="itest",
            now=lambda: clock["t"],
            idle_seconds=60.0,
            probe_interval_seconds=1.0,
        )
        task = _task(task_id)
        clock["t"] += 120.0  # would have crossed the idle threshold if not for the guard
        monitor.tick(task)
        clock["t"] += 2.0
        monitor.tick(task)
        clock["t"] += 2.0
        monitor.tick(task)

        assert client.phases == []
        assert spawner.respawned == []
    finally:
        runner.stop(container)


def test_recovery_cap_surfaces_loudly_then_a_manual_bump_clears_it(image: str) -> None:
    task_id = "stall-d"
    runner, container = _spawn(image, task_id, extra_env={"FAKE_CLAUDE_MODE": "unresponsive"})
    try:
        clock = {"t": time.time()}
        client, spawner = _FakeClient(), _FakeSpawner()
        monitor = StallMonitor(
            client,  # type: ignore[arg-type]
            runner,
            spawner,  # type: ignore[arg-type]
            runner_id="itest",
            now=lambda: clock["t"],
            idle_seconds=60.0,
            probe_interval_seconds=1.0,
            max_recoveries=1,
        )
        task = _task(task_id)

        clock["t"] += 120.0
        monitor.tick(task)  # baseline
        clock["t"] += 2.0
        monitor.tick(task)  # recovery #1 (the only one allowed) — fake-claude ignores it
        assert client.phases[-1][1] == "stalled"
        assert "auto-retry 1/1" in (client.phases[-1][2] or "")

        clock["t"] += 200.0  # past backoff(1) = 120s
        monitor.tick(task)  # cap reached — surfaced loudly, not attempted again
        _, phase, detail = client.phases[-1]
        assert phase == "stalled"
        assert detail is not None
        assert "1/1 auto-recoveries exhausted" in detail
        assert "needs manual bump" in detail

        # a manual bump: an operator (or us, simulating one) types directly into the pane —
        # fake-claude in "unresponsive" mode still won't advance the transcript, but a *real*
        # agent would; simulate that half by writing a fresh transcript entry directly, the same
        # observable effect a real recovery has.
        subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "panopticon",
                container,
                "sh",
                "-c",
                'echo \'{"type":"assistant","message":{"model":"claude","usage":{}}}\''
                " >> /home/panopticon/.claude/projects/-workspace/session.jsonl",
            ],
            check=True,
        )
        clock["t"] += 2.0
        monitor.tick(task)  # fresh transcript activity — resolves and clears
        assert client.cleared[-1] == task_id
    finally:
        runner.stop(container)
