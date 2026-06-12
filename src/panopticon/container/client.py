"""A thin REST client for the task service, used from inside a task container.

Wraps an :class:`httpx.Client` (real, pointed at the runner-injected service URL; or a
FastAPI ``TestClient`` in tests). Skills and the entrypoint use this; agents also have the
MCP surface (later slice).
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
    def _json(resp: httpx.Response) -> JsonObj:
        resp.raise_for_status()
        return cast(JsonObj, resp.json())

    # -- repos / tasks ------------------------------------------------------------

    def create_repo(self, repo_id: str, name: str, default_base: str = "main") -> JsonObj:
        return self._json(
            self._http.post(
                "/repos", json={"id": repo_id, "name": name, "default_base": default_base}
            )
        )

    def create_task(self, repo_id: str, workflow: str) -> JsonObj:
        return self._json(
            self._http.post("/tasks", json={"repo_id": repo_id, "workflow": workflow})
        )

    def get_task(self, task_id: str) -> JsonObj:
        return self._json(self._http.get(f"/tasks/{task_id}"))

    def set_slug(self, task_id: str, slug: str) -> JsonObj:
        return self._json(self._http.put(f"/tasks/{task_id}/slug", json={"slug": slug}))

    def request_transition(
        self,
        task_id: str,
        to_state: str,
        *,
        trigger: str | None = None,
        note: str | None = None,
    ) -> JsonObj:
        body: JsonObj = {"to_state": to_state, "trigger": trigger, "note": note}
        return self._json(self._http.post(f"/tasks/{task_id}/transition", json=body))

    def resolve_responsibility(
        self, task_id: str, key: str, status: Status, comment: str | None = None
    ) -> JsonObj:
        """Resolve one of the current state's promised responsibilities (MET or FAILED)."""
        body: JsonObj = {"key": key, "status": status.value, "comment": comment}
        return self._json(self._http.post(f"/tasks/{task_id}/responsibilities", json=body))

    # -- artifacts ----------------------------------------------------------------

    def put_artifact(self, task_id: str, name: str, content: bytes) -> None:
        self._http.put(f"/tasks/{task_id}/artifacts/{name}", content=content).raise_for_status()

    def get_artifact(self, task_id: str, name: str) -> bytes:
        resp = self._http.get(f"/tasks/{task_id}/artifacts/{name}")
        resp.raise_for_status()
        return resp.content

    # -- liveness -----------------------------------------------------------------

    def register(self, task_id: str, container_id: str, runner_id: str | None = None) -> JsonObj:
        return self._json(
            self._http.post(
                f"/tasks/{task_id}/registrations",
                json={"container_id": container_id, "runner_id": runner_id},
            )
        )

    def heartbeat(self, registration_id: str) -> JsonObj:
        return self._json(self._http.post(f"/registrations/{registration_id}/heartbeat"))

    def deregister(self, registration_id: str) -> None:
        self._http.delete(f"/registrations/{registration_id}").raise_for_status()

    def list_registrations(self, task_id: str) -> list[JsonObj]:
        resp = self._http.get(f"/tasks/{task_id}/registrations")
        resp.raise_for_status()
        return cast("list[JsonObj]", resp.json())
