"""The host-side ask worker (ask-the-author): delivers a task's pending question to its agent.

Unit tests pin the delivery decision — inject into a live session, resume a parked/terminal one via
``--continue``, mark a reaped-volume ask gone, and no-op an undelivered-free or other-host task — with
fake runner/spawner and the real task service over REST (so the recorded ask status is authoritative).
An integration test proves the headline path end to end: create a task, ask, observe delivery, then
(standing in for the container Stop hook) record the answer and retrieve it. No Docker, no LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.asking import ask_marker
from panopticon.core.models import Repo
from panopticon.sessionservice.ask_worker import AskWorker
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


class _FakeRunner:
    """Records live-inject calls; volume/session presence are configurable per test."""

    def __init__(self, *, volume: bool = True, session: bool = False) -> None:
        self.volume = volume
        self.session = session
        self.sent: list[tuple[str, str]] = []

    def config_volume_exists(self, task_id: str) -> bool:
        return self.volume

    def has_session(self, task_id: str) -> bool:
        return self.session

    def send_to_session(self, task_id: str, text: str) -> None:
        self.sent.append((task_id, text))


class _FakeSpawner:
    """Records spawn_for_ask (parked/terminal resume) calls; the claim result is configurable."""

    def __init__(self, *, result: str | None = "panopticon-c1") -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def spawn_for_ask(self, task: JsonObj, ask_prompt: str) -> str | None:
        self.calls.append((task["id"], ask_prompt))
        return self.result


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TaskServiceClient]:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


def _task_with_ask(client: TaskServiceClient, *, question: str = "why?") -> tuple[JsonObj, str]:
    """Create a task, post an ask, and return the task snapshot (carrying pending_ask_id) + ask id."""
    task_id = client.create_task("r1", "spike")["id"]
    ask_id = client.create_ask(task_id, question)
    return client.get_task(task_id), ask_id


def _worker(client: TaskServiceClient, runner: _FakeRunner, spawner: _FakeSpawner) -> AskWorker:
    return AskWorker(client, runner, spawner, runner_id="local")  # type: ignore[arg-type]


def test_deliver_injects_into_a_live_session(client: TaskServiceClient) -> None:
    task, ask_id = _task_with_ask(client, question="why a dict?")
    runner, spawner = _FakeRunner(volume=True, session=True), _FakeSpawner()
    assert _worker(client, runner, spawner).deliver(task) == ask_id
    # Injected into the live pane (not respawned), and the message carries the marker + question.
    assert len(runner.sent) == 1 and spawner.calls == []
    _, message = runner.sent[0]
    assert ask_marker(ask_id) in message and "why a dict?" in message
    # The ask is now delivered → no longer the task's pending ask.
    assert client.get_task(task["id"])["pending_ask_id"] is None
    assert client.get_ask(task["id"], ask_id)["status"] == "pending"  # delivered reads as pending


def test_deliver_resumes_a_parked_task_via_continue(client: TaskServiceClient) -> None:
    task, ask_id = _task_with_ask(client)
    runner, spawner = _FakeRunner(volume=True, session=False), _FakeSpawner()
    assert _worker(client, runner, spawner).deliver(task) == ask_id
    # No live session → resumed with the ask as the --continue prompt; nothing injected.
    assert runner.sent == [] and len(spawner.calls) == 1
    assert spawner.calls[0][0] == task["id"] and ask_marker(ask_id) in spawner.calls[0][1]
    assert client.get_task(task["id"])["pending_ask_id"] is None


def test_deliver_to_a_complete_task_uses_the_readonly_guardrail(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    client.set_state(task_id, "COMPLETE")  # terminal — asking its author must still work
    ask_id = client.create_ask(task_id, "what changed?")
    task = client.get_task(task_id)
    runner, spawner = _FakeRunner(volume=True, session=False), _FakeSpawner()

    assert _worker(client, runner, spawner).deliver(task) == ask_id
    # Resumed (spawn_for_ask allows terminal) with the strict read-only framing.
    assert len(spawner.calls) == 1
    message = spawner.calls[0][1]
    assert "merged or proposed work" in message and "not a request to change anything" in message
    assert client.get_task(task_id)["state"] == "COMPLETE"  # untouched


def test_deliver_marks_gone_when_the_volume_is_reaped(client: TaskServiceClient) -> None:
    task, ask_id = _task_with_ask(client)
    runner, spawner = _FakeRunner(volume=False), _FakeSpawner()
    assert _worker(client, runner, spawner).deliver(task) is None
    assert runner.sent == [] and spawner.calls == []
    # Marked gone → the poll returns 410.
    assert client._http.get(f"/tasks/{task['id']}/ask/{ask_id}").status_code == 410


def test_deliver_is_a_noop_without_a_pending_ask(client: TaskServiceClient) -> None:
    task_id = client.create_task("r1", "spike")["id"]
    task = client.get_task(task_id)  # no ask → pending_ask_id is None
    runner, spawner = _FakeRunner(volume=True, session=True), _FakeSpawner()
    assert _worker(client, runner, spawner).deliver(task) is None
    assert runner.sent == [] and spawner.calls == []


def test_deliver_skips_a_task_owned_by_another_host(client: TaskServiceClient) -> None:
    task, _ = _task_with_ask(client)
    task = {**task, "claimed_by": "other-host"}  # another runner owns it — it delivers there
    runner, spawner = _FakeRunner(volume=True, session=True), _FakeSpawner()
    assert _worker(client, runner, spawner).deliver(task) is None
    assert runner.sent == [] and spawner.calls == []


def test_deliver_leaves_ask_pending_when_it_cannot_claim(client: TaskServiceClient) -> None:
    # A parked task the worker can't claim (another host won the race) stays pending for retry.
    task, ask_id = _task_with_ask(client)
    runner, spawner = _FakeRunner(volume=True, session=False), _FakeSpawner(result=None)
    assert _worker(client, runner, spawner).deliver(task) is None
    assert client.get_task(task["id"])["pending_ask_id"] == ask_id  # still pending


def test_end_to_end_ask_delivery_and_answer(client: TaskServiceClient) -> None:
    # The memo's headline: create a task, ask, observe delivery, then (as the container Stop hook
    # would) record the reply and retrieve it.
    task, ask_id = _task_with_ask(client, question="why memoize?")
    runner, spawner = _FakeRunner(volume=True, session=True), _FakeSpawner()
    _worker(client, runner, spawner).deliver(task)

    # The agent answered; its Stop hook records the reply extracted from the transcript.
    client.record_ask_answer(task["id"], ask_id, "to avoid recomputation")
    answered = client.get_ask(task["id"], ask_id)
    assert answered["status"] == "answered" and answered["answer"] == "to avoid recomputation"


def test_queued_asks_deliver_strictly_serially_with_correct_attribution(
    client: TaskServiceClient,
) -> None:
    # Three questions queued on one task are delivered one at a time: the worker delivers the head,
    # then holds until that ask is answered before delivering the next, and the Stop-hook attribution
    # (outstanding_ask) names exactly the in-flight ask at each step. This is why delivery must be
    # serialized — a second marker injected mid-answer would truncate the first reply.
    task_id = client.create_task("r1", "spike")["id"]
    ids = [client.create_ask(task_id, f"q{i}?") for i in range(3)]
    runner, spawner = _FakeRunner(volume=True, session=True), _FakeSpawner()
    worker = _worker(client, runner, spawner)

    for i, ask_id in enumerate(ids):
        task = client.get_task(task_id)
        assert task["pending_ask_id"] == ask_id  # only the head is deliverable
        assert worker.deliver(task) == ask_id
        assert len(runner.sent) == i + 1  # exactly one delivery this round
        assert ask_marker(ask_id) in runner.sent[-1][1]  # the right question's marker

        # While it's in flight, nothing else is deliverable and attribution names this ask.
        assert client.get_task(task_id)["pending_ask_id"] is None
        assert worker.deliver(client.get_task(task_id)) is None  # queue held
        assert len(runner.sent) == i + 1
        assert client.outstanding_ask(task_id) == ask_id

        client.record_ask_answer(task_id, ask_id, f"answer {i}")  # the Stop hook fires

    # All three answered, in order, each attributed to its own question.
    assert [client.get_ask(task_id, a)["answer"] for a in ids] == [
        "answer 0",
        "answer 1",
        "answer 2",
    ]


def test_gone_drains_the_whole_queue(client: TaskServiceClient) -> None:
    # A reaped volume is unrecoverable for every queued question, not just the head — so delivering
    # into a dead session drains the whole queue GONE at once (the review tool offers a surrogate
    # once, not per question).
    task_id = client.create_task("r1", "spike")["id"]
    ids = [client.create_ask(task_id, f"q{i}?") for i in range(3)]
    runner, spawner = _FakeRunner(volume=False), _FakeSpawner()  # volume gone

    assert _worker(client, runner, spawner).deliver(client.get_task(task_id)) is None
    assert runner.sent == [] and spawner.calls == []
    for a in ids:  # every queued ask is gone → 410
        assert client._http.get(f"/tasks/{task_id}/ask/{a}").status_code == 410
