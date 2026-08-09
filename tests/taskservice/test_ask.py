"""ask-the-author over REST: the tarot review tool's contract with the task service.

Covers the API the memo pins: ``GET /tasks/lookup`` (resolve a task by branch/url), ``POST
/tasks/{id}/ask`` (post a question, capped at one unanswered per task), ``GET /tasks/{id}/ask/{id}``
(poll for the answer), the COMPLETE-without-transition guarantee, and the dead-volume → 410 fallback.
Delivery + answer extraction happen in the session service + container; here we drive their recorded
outcomes over the client to prove the control-plane contract. No Docker, no LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TaskServiceClient]:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="r1", name="acme/widgets", git_url="https://x/r1.git")))
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)


def _task(client: TaskServiceClient) -> str:
    return client.create_task("r1", "spike")["id"]


def test_ask_then_retrieve_answer(client: TaskServiceClient) -> None:
    # Post a question, observe it surface as the task's pending ask (what the session service's ask
    # worker keys on), then — standing in for the session service + container Stop hook — mark it
    # delivered and record the answer, and confirm the poll returns it.
    task_id = _task(client)
    ask_id = client.create_ask(task_id, "why a dict here?", context="reviewing models.py")

    # Before delivery the ask is the task's pending ask and the poll reads `pending`.
    assert client.get_task(task_id)["pending_ask_id"] == ask_id
    pending = client.get_ask(task_id, ask_id)
    assert pending["status"] == "pending" and pending["answer"] is None
    assert pending["question"] == "why a dict here?" and pending["context"] == "reviewing models.py"

    # The session service delivers it → no longer pending; the poll still reads `pending` (the wire
    # only distinguishes pending vs answered — `delivered` is internal).
    client.mark_ask_delivered(task_id, ask_id)
    assert client.get_task(task_id)["pending_ask_id"] is None
    assert client.get_ask(task_id, ask_id)["status"] == "pending"

    # The container Stop hook records the agent's reply → the poll reads `answered` with the text.
    client.record_ask_answer(task_id, ask_id, "because lookups are by id")
    answered = client.get_ask(task_id, ask_id)
    assert answered["status"] == "answered"
    assert answered["answer"] == "because lookups are by id"


def test_ask_is_capped_at_one_unanswered_per_task(client: TaskServiceClient) -> None:
    task_id = _task(client)
    client.create_ask(task_id, "first?")
    # A second concurrent ask is rejected while the first is unanswered (cap: 1 per task).
    resp = client._http.post(f"/tasks/{task_id}/ask", json={"question": "second?"})
    assert resp.status_code == 409
    # Once the first is answered, a new ask is allowed again.
    first = client.outstanding_ask(task_id)
    assert first is not None
    client.record_ask_answer(task_id, first, "yes")
    second = client.create_ask(task_id, "second?")
    assert second != first


def test_ask_on_a_complete_task_answers_without_transition(client: TaskServiceClient) -> None:
    # The headline guardrail: asking a COMPLETE task's agent must work and must NOT restart the
    # workflow — no state change, no new history entry, no responsibilities.
    task_id = _task(client)
    client.set_state(task_id, "COMPLETE")  # free move to the terminal state
    before = client.get_task(task_id)
    assert before["state"] == "COMPLETE"
    history_len = len(before["history"])

    ask_id = client.create_ask(task_id, "what did you change in the store?")
    client.record_ask_answer(task_id, ask_id, "added two lookup queries")

    after = client.get_task(task_id)
    assert client.get_ask(task_id, ask_id)["status"] == "answered"
    assert after["state"] == "COMPLETE"  # still terminal — the ask was conversation, not a move
    assert len(after["history"]) == history_len  # no transition recorded


def test_ask_with_a_reaped_volume_returns_410(client: TaskServiceClient) -> None:
    # If the config volume is gone (reaped), the ask can't be delivered. The session service marks
    # it gone and the poll returns 410 — the documented signal for the review tool's fallback.
    task_id = _task(client)
    ask_id = client.create_ask(task_id, "still around?")
    client.mark_ask_gone(task_id, ask_id)  # the ask worker's volume-gone outcome

    resp = client._http.get(f"/tasks/{task_id}/ask/{ask_id}")
    assert resp.status_code == 410
    with pytest.raises(httpx.HTTPStatusError):
        client.get_ask(task_id, ask_id)


def test_ask_on_unknown_task_is_404(client: TaskServiceClient) -> None:
    resp = client._http.post("/tasks/nope/ask", json={"question": "hi?"})
    assert resp.status_code == 404


def test_lookup_by_branch(client: TaskServiceClient) -> None:
    task_id = _task(client)
    client.set_slug(task_id, "fix-widget")
    client.record_provisioning(task_id, "panopticon/fix-widget", f"/clones/{task_id}")

    found = client.lookup_task(repo_id="r1", branch="panopticon/fix-widget")
    assert found is not None and found["id"] == task_id
    # A branch that no task holds → 404 → None.
    assert client.lookup_task(repo_id="r1", branch="panopticon/absent") is None


def test_lookup_by_url(client: TaskServiceClient) -> None:
    task_id = _task(client)
    client.set_url(task_id, "https://forge/pr/7")

    found = client.lookup_task(url="https://forge/pr/7")
    assert found is not None and found["id"] == task_id
    assert client.lookup_task(url="https://forge/pr/999") is None


def test_lookup_requires_a_valid_selector(client: TaskServiceClient) -> None:
    # Neither branch nor url → 400 (a malformed request, not a miss).
    assert client._http.get("/tasks/lookup").status_code == 400
    # Mixing url with branch is rejected too.
    assert (
        client._http.get(
            "/tasks/lookup", params={"url": "u", "repo_id": "r1", "branch": "b"}
        ).status_code
        == 400
    )


def test_lookup_is_not_shadowed_by_the_task_id_route(client: TaskServiceClient) -> None:
    # `/tasks/lookup` must resolve to the lookup handler, not be captured as task_id="lookup".
    resp = client._http.get("/tasks/lookup", params={"url": "https://none"})
    assert resp.status_code == 404  # a clean "no match", not a 200 task nor a validation error
