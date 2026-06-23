"""The long-lived container entrypoint: register → slug → heartbeat → deregister.

Uses a fake client — no network, no Docker, no LLM (the agent step is a stay-alive loop)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
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
    naps: list[float] = []
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://svc:8000")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "t1")
    monkeypatch.setenv("PANOPTICON_CONTAINER_ID", "panopticon-t1")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "local")
    monkeypatch.setenv("PANOPTICON_HEARTBEAT_INTERVAL", "0.5")

    def factory(url: str) -> _FakeClient:
        seen_url.append(url)
        return client

    entrypoint.main(
        client_factory=factory,  # type: ignore[arg-type]
        running=_stop_after(1),
        sleep=naps.append,
    )
    assert seen_url == ["http://svc:8000"]
    assert client.calls[0] == "register"
    assert client.calls[-1] == "deregister"
    assert naps == [0.5]  # PANOPTICON_HEARTBEAT_INTERVAL threaded through


class _FlakyRegisterClient(_FakeClient):
    """register raises a transient ConnectError ``fails`` times, then succeeds (a momentarily
    unreachable service at boot — the "takes multiple tries to start" case)."""

    def __init__(self, *, fails: int) -> None:
        super().__init__()
        self._left = fails

    def register(self, task_id: str, *, container_id: str, runner_id: str | None = None) -> dict[str, Any]:
        if self._left > 0:
            self._left -= 1
            self.calls.append("register-fail")
            raise httpx.ConnectError("connection refused")
        return super().register(task_id, container_id=container_id, runner_id=runner_id)


def test_serve_retries_register_until_the_service_is_reachable() -> None:
    client = _FlakyRegisterClient(fails=3)  # refused 3×, then up
    naps: list[float] = []
    entrypoint.serve(
        client, "t1", container_id="c1", running=_stop_after(1),  # type: ignore[arg-type]
        register_interval=0.0, sleep=naps.append,
    )
    assert client.calls.count("register-fail") == 3  # retried through the blips...
    assert "register" in client.calls and client.heartbeats == 1  # ...then booted normally
    assert len(naps) >= 3  # backed off between attempts


def test_serve_gives_up_register_after_the_attempt_budget() -> None:
    client = _FlakyRegisterClient(fails=100)  # service never comes up
    with pytest.raises(httpx.TransportError):
        entrypoint.serve(
            client, "t1", container_id="c1", running=_stop_after(1),  # type: ignore[arg-type]
            register_attempts=3, register_interval=0.0, sleep=lambda _s: None,
        )


def test_serve_does_not_retry_a_non_transport_register_error() -> None:
    client = _FakeClient()

    def boom(*_a: object, **_k: object) -> dict[str, Any]:
        raise RuntimeError("genuine error")  # not a TransportError → propagate immediately

    client.register = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        entrypoint.serve(
            client, "t1", container_id="c1", running=_stop_after(1),  # type: ignore[arg-type]
            sleep=lambda _s: None,
        )


def test_serve_tolerates_a_transient_heartbeat_failure() -> None:
    client = _FakeClient()
    real_heartbeat = client.heartbeat
    state = {"first": True}

    def flaky_heartbeat(registration_id: str) -> dict[str, Any]:
        if state["first"]:
            state["first"] = False
            raise httpx.ConnectError("blip")  # one transient failure mid-life
        return real_heartbeat(registration_id)

    client.heartbeat = flaky_heartbeat  # type: ignore[assignment]
    _serve(client, running=_stop_after(2))  # must not crash on the blip
    assert client.heartbeats == 1  # the second beat went through
    assert client.calls[-1] == "deregister"  # survived and shut down cleanly
