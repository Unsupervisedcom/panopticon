"""WorkflowExecutions: the shared "how is this workflow run" cache — fetches each spec once and
answers is_shell. No HTTP; a fake client counts calls."""

from __future__ import annotations

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


def test_is_shell_is_false_for_a_missing_workflow_name() -> None:
    # Callers pass a task's `workflow` straight through; None/empty must not hit the client.
    client = _FakeClient({})
    execs = WorkflowExecutions(client)  # type: ignore[arg-type]
    assert execs.is_shell(None) is False
    assert execs.is_shell("") is False
    assert client.calls == []
