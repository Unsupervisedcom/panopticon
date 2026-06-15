"""The long-lived container entrypoint: register → slug → heartbeat → deregister.

Uses a fake client — no network, no Docker, no LLM (the agent step is a stay-alive loop)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from panopticon.container import entrypoint


class _FakeClient:
    """Records the entrypoint's calls; stands in for TaskServiceClient."""

    def __init__(self, slug: str | None = None) -> None:
        self.slug = slug
        self.calls: list[str] = []
        self.heartbeats = 0

    def register(self, task_id: str, *, container_id: str, runner_id: str | None = None) -> dict[str, Any]:
        self.calls.append("register")
        return {"id": "reg1"}

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"slug": self.slug}

    def set_slug(self, task_id: str, slug: str) -> dict[str, Any]:
        self.calls.append("set_slug")
        self.slug = slug
        return {"slug": slug}

    def heartbeat(self, registration_id: str) -> dict[str, Any]:
        self.heartbeats += 1
        self.calls.append("heartbeat")
        return {}

    def deregister(self, registration_id: str) -> None:
        self.calls.append("deregister")


def _stop_after(n: int) -> Callable[[], bool]:
    """A ``running`` predicate true for ``n`` iterations, then false."""
    seen = 0

    def running() -> bool:
        nonlocal seen
        seen += 1
        return seen <= n

    return running


def _serve(client: _FakeClient, **kw: Any) -> None:
    entrypoint.serve(
        client,  # type: ignore[arg-type]
        "t1",
        container_id="c1",
        running=kw.pop("running", _stop_after(2)),
        sleep=lambda _s: None,
        **kw,
    )


def test_serve_registers_heartbeats_and_deregisters() -> None:
    client = _FakeClient()
    _serve(client)
    assert client.heartbeats == 2
    assert client.calls[0] == "register"
    assert client.calls[-1] == "deregister"


def test_serve_sets_slug_when_unset_and_proposed() -> None:
    client = _FakeClient(slug=None)
    _serve(client, proposed_slug="fix-widget", running=_stop_after(1))
    assert client.slug == "fix-widget"
    assert "set_slug" in client.calls


def test_serve_leaves_existing_slug_alone() -> None:
    client = _FakeClient(slug="chosen")
    _serve(client, proposed_slug="other", running=_stop_after(1))
    assert client.slug == "chosen"
    assert "set_slug" not in client.calls


def test_serve_deregisters_even_on_error() -> None:
    client = _FakeClient()

    def boom() -> bool:
        raise RuntimeError("kill")

    with pytest.raises(RuntimeError):
        _serve(client, running=boom)
    assert client.calls[-1] == "deregister"  # finally ran


def test_main_reads_env_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    seen_url: list[str] = []
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc:8000")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("PANOPTICON_CONTAINER_ID", "panopticon-t1")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "local")

    def factory(url: str) -> _FakeClient:
        seen_url.append(url)
        return client

    entrypoint.main(
        client_factory=factory,  # type: ignore[arg-type]
        running=_stop_after(1),
        sleep=lambda _s: None,
    )
    assert seen_url == ["http://svc:8000"]
    assert client.calls[0] == "register"
    assert client.calls[-1] == "deregister"
