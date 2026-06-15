"""The task service's REST client — shared by every caller outside the service itself.

Both the in-container agent harness and the terminal controller (dashboard + CLI) talk to the
task service over the same HTTP API, so they share one client. It wraps an
:class:`httpx.Client` — real (pointed at the runner-injected service URL) or a FastAPI
``TestClient`` in tests. Methods mirror the API one-for-one; reads return parsed JSON, writes
return the updated resource. LLM-free — agents reach the LLM only inside the container.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from panopticon.core.models import Status

JsonObj = dict[str, Any]


class TaskServiceClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        resp.raise_for_status()
        return resp.json()

    # -- reads --------------------------------------------------------------------

    def list_workflows(self) -> list[str]:
        return cast("list[str]", self._json(self._http.get("/workflows")))

    def list_repos(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/repos")))

    def list_tasks(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/tasks")))

    def get_task(self, task_id: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.get(f"/tasks/{task_id}")))

    def list_transitions(self, task_id: str) -> list[str]:
        return cast("list[str]", self._json(self._http.get(f"/tasks/{task_id}/transitions")))

    def list_operations(self, task_id: str) -> dict[str, str]:
        return cast("dict[str, str]", self._json(self._http.get(f"/tasks/{task_id}/operations")))

    def list_registrations(self, task_id: str) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get(f"/tasks/{task_id}/registrations")))

    # -- repos / tasks ------------------------------------------------------------

    def create_repo(self, repo_id: str, name: str, git_url: str, default_base: str = "main") -> JsonObj:
        body = {"id": repo_id, "name": name, "git_url": git_url, "default_base": default_base}
        return cast(JsonObj, self._json(self._http.post("/repos", json=body)))

    def create_task(self, repo_id: str, workflow: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.post("/tasks", json={"repo_id": repo_id, "workflow": workflow})))

    def set_slug(self, task_id: str, slug: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/slug", json={"slug": slug})))

    def request_transition(
        self, task_id: str, to_state: str, *, trigger: str | None = None, note: str | None = None
    ) -> JsonObj:
        body: JsonObj = {"to_state": to_state, "trigger": trigger, "note": note}
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/transition", json=body)))

    def apply_operation(self, task_id: str, operation: str) -> JsonObj:
        """Apply a named core operation (e.g. advance/iterate/drop); the workflow resolves the target."""
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/operations/{operation}")))

    def resolve_responsibility(
        self, task_id: str, key: str, status: Status, comment: str | None = None
    ) -> JsonObj:
        """Resolve one of the current state's promised responsibilities (MET or FAILED)."""
        body: JsonObj = {"key": key, "status": status.value, "comment": comment}
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/responsibilities", json=body)))

    # -- artifacts ----------------------------------------------------------------

    def put_artifact(self, task_id: str, name: str, content: bytes) -> None:
        self._http.put(f"/tasks/{task_id}/artifacts/{name}", content=content).raise_for_status()

    def get_artifact(self, task_id: str, name: str) -> bytes:
        resp = self._http.get(f"/tasks/{task_id}/artifacts/{name}")
        resp.raise_for_status()
        return resp.content

    # -- liveness -----------------------------------------------------------------

    def register(self, task_id: str, container_id: str, runner_id: str | None = None) -> JsonObj:
        return cast(
            JsonObj,
            self._json(
                self._http.post(
                    f"/tasks/{task_id}/registrations",
                    json={"container_id": container_id, "runner_id": runner_id},
                )
            ),
        )

    def heartbeat(self, registration_id: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.post(f"/registrations/{registration_id}/heartbeat")))

    def deregister(self, registration_id: str) -> None:
        self._http.delete(f"/registrations/{registration_id}").raise_for_status()
