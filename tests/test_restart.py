"""`restart_repo_containers`: restart a repo's live task containers (stop + release → respawn) so
they pick up freshly-written login credentials. Fakes for the client + runner; no docker/REST."""

from __future__ import annotations

from panopticon.sessionservice.restart import restart_repo_containers


class _Client:
    def __init__(self, tasks: list[dict[str, object]], registrations: dict[str, list[dict[str, str]]]):
        self._tasks = tasks
        self._registrations = registrations
        self.released: list[str] = []

    def list_tasks(self) -> list[dict[str, object]]:
        return self._tasks

    def list_registrations(self, task_id: str) -> list[dict[str, str]]:
        return self._registrations.get(task_id, [])

    def release(self, task_id: str) -> dict[str, object]:
        self.released.append(task_id)
        return {}


class _Runner:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


def test_restart_restarts_only_live_non_terminal_repo_containers() -> None:
    client = _Client(
        tasks=[
            {"id": "t1", "repo_id": "r1", "state": "ITERATING"},  # live → restart
            {"id": "t2", "repo_id": "r1", "state": "PLANNING"},   # live → restart
            {"id": "t3", "repo_id": "r2", "state": "ITERATING"},  # other repo → skip
            {"id": "t4", "repo_id": "r1", "state": "COMPLETE"},   # terminal → skip
            {"id": "t5", "repo_id": "r1", "state": "ITERATING"},  # no live container → skip
        ],
        registrations={
            "t1": [{"container_id": "panopticon-t1"}],
            "t2": [{"container_id": "panopticon-t2"}],
            "t5": [],
        },
    )
    runner = _Runner()

    restarted = restart_repo_containers(client, runner, "r1")  # type: ignore[arg-type]

    assert restarted == ["t1", "t2"]
    assert runner.stopped == ["panopticon-t1", "panopticon-t2"]  # stop kills the session + container
    assert client.released == ["t1", "t2"]  # released → the host daemon respawns each with fresh creds


def test_restart_is_a_noop_when_no_repo_containers_are_live() -> None:
    client = _Client(
        tasks=[{"id": "t1", "repo_id": "r1", "state": "ITERATING"}],
        registrations={"t1": []},
    )
    runner = _Runner()

    assert restart_repo_containers(client, runner, "r1") == []  # type: ignore[arg-type]
    assert runner.stopped == [] and client.released == []
