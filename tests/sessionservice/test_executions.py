"""WorkflowExecutions: the shared "how is this workflow run" cache — fetches each spec once and
answers is_shell. No HTTP; a fake client counts calls."""

from __future__ import annotations

import httpx
import pytest

from panopticon.client import JsonObj
from panopticon.sessionservice.executions import WorkflowExecutions


class _FakeClient:
    def __init__(self, specs: dict[str, JsonObj]) -> None:
        self._specs = specs
        self.calls: list[str] = []

    def workflow_execution(self, name: str) -> JsonObj:
        self.calls.append(name)
        return self._specs[name]


def test_fetches_once_then_caches() -> None:
    client = _FakeClient(
        {"wf": {"runner_type": "docker", "script": "", "clone_repo": False, "workdir": None}}
    )
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.spec("wf")["runner_type"] == "docker"
    assert execs.spec("wf")["runner_type"] == "docker"
    assert client.calls == ["wf"]  # second lookup is served from cache


def test_is_shell_reflects_runner_type() -> None:
    client = _FakeClient(
        {
            "sh": {
                "runner_type": "shell",
                "script": "echo hi",
                "clone_repo": False,
                "workdir": None,
            },
            "dk": {"runner_type": "docker", "script": "", "clone_repo": False, "workdir": None},
        }
    )
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_shell("sh") is True
    assert execs.is_shell("dk") is False


def test_is_forge_reflects_runner_type() -> None:
    client = _FakeClient({"resident-agent": {"runner_type": "forge"}})
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_forge("resident-agent") is True
    assert execs.is_forge(None) is False


def test_is_unmanaged_covers_shell_and_forge() -> None:
    client = _FakeClient(
        {
            "sh": {"runner_type": "shell"},
            "resident-agent": {"runner_type": "forge"},
            "dk": {"runner_type": "docker"},
        }
    )
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_unmanaged("sh") is True
    assert execs.is_unmanaged("resident-agent") is True
    assert execs.is_unmanaged("dk") is False
    assert execs.is_unmanaged(None) is False


def test_is_shell_is_false_for_a_missing_workflow_name() -> None:
    # Callers pass a task's `workflow` straight through; None/empty must not hit the client.
    client = _FakeClient({})
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_shell(None) is False
    assert execs.is_shell("") is False
    assert client.calls == []


class _FakeClientRaises:
    """Client that raises HTTPStatusError(status) for any workflow_execution call."""

    def __init__(self, status: int) -> None:
        self.calls: list[str] = []
        self._status = status

    def workflow_execution(self, name: str) -> JsonObj:
        self.calls.append(name)
        request = httpx.Request("GET", f"http://svc/workflows/{name}/execution")
        raise httpx.HTTPStatusError(
            "error", request=request, response=httpx.Response(self._status, request=request)
        )


def test_spec_returns_docker_fallback_for_unknown_workflow() -> None:
    # A workflow name no longer in the registry → 400 → fallback spec cached and returned.
    client = _FakeClientRaises(400)
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    spec = execs.spec("parity")
    assert spec["runner_type"] == "docker"
    assert spec["clone_repo"] is False
    assert spec["poll_interval_seconds"] is None
    assert spec["pr_timeout_seconds"] is None
    # Second call is served from cache — no second HTTP hit.
    spec2 = execs.spec("parity")
    assert spec2 is spec
    assert client.calls == ["parity"]


def test_is_shell_returns_false_for_unknown_workflow() -> None:
    client = _FakeClientRaises(400)
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_shell("parity") is False


def test_spec_reraises_server_errors() -> None:
    # 5xx is a transient service failure, not a missing workflow — must still propagate.
    client = _FakeClientRaises(503)
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    with pytest.raises(httpx.HTTPStatusError):
        execs.spec("some-workflow")


def test_is_kubernetes_and_operator_agent_route_a_kubernetes_workflow() -> None:
    client = _FakeClient(
        {
            "k8s": {
                "runner_type": "kubernetes",
                "script": "",
                "clone_repo": False,
                "workdir": None,
                "operator_agent": "researcher",
            }
        }
    )
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]

    assert execs.is_kubernetes("k8s") is True
    assert execs.is_shell("k8s") is False
    assert execs.operator_agent("k8s") == "researcher"


def test_a_kubernetes_workflow_without_an_agent_is_refused_not_guessed() -> None:
    """The workflow class already refuses to be defined this way, so reaching here means the host
    and the task service disagree about the registry — better loud than spawned somewhere random."""
    client = _FakeClient(
        {
            "k8s": {
                "runner_type": "kubernetes",
                "script": "",
                "clone_repo": False,
                "workdir": None,
                "operator_agent": None,
            }
        }
    )
    with pytest.raises(ValueError, match="declares no operator_agent"):
        WorkflowExecutions(client).operator_agent("k8s")  # type: ignore[arg-type]
