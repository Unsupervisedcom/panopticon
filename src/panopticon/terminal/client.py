"""A REST client for the terminal controller (dashboard + CLI).

Wraps an :class:`httpx.Client` pointed at the task service. Read operations feed the
dashboard; write operations drive it (create/advance tasks, set slug). It overlaps the
in-container client (:mod:`panopticon.container.client`); the two are unified into a shared
task-service client in the final PR of this slice.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

JsonObj = dict[str, Any]


class DashboardClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        resp.raise_for_status()
        return resp.json()

    # -- reads (feed the dashboard) -----------------------------------------------

    def list_workflows(self) -> list[str]:
        return cast("list[str]", self._json(self._http.get("/workflows")))

    def list_repos(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/repos")))

    def list_tasks(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/tasks")))

    def get_task(self, task_id: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.get(f"/tasks/{task_id}")))

    def list_registrations(self, task_id: str) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get(f"/tasks/{task_id}/registrations")))

    def list_transitions(self, task_id: str) -> list[str]:
        return cast("list[str]", self._json(self._http.get(f"/tasks/{task_id}/transitions")))

    # -- writes (drive the dashboard) ---------------------------------------------

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
        body = {"to_state": to_state, "trigger": trigger, "note": note}
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/transition", json=body)))
