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

    def workflow_image_layer(self, name: str) -> str:
        """The workflow's Dockerfile layer (ADR 0005); empty when it needs none."""
        body = cast(JsonObj, self._json(self._http.get(f"/workflows/{name}/image-layer")))
        return cast(str, body["layer"])

    def get_repo(self, repo_id: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.get(f"/repos/{repo_id}")))

    def list_repos(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/repos")))

    def list_tasks(self) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get("/tasks")))

    def list_tasks_versioned(
        self, *, since: int = 0, wait: float | None = None
    ) -> tuple[list[JsonObj], int]:
        """Block-until-change ``GET /tasks``: return ``(tasks, version)`` where ``version`` is the
        store's change-feed cursor (the ``X-Tasks-Version`` header).

        With ``wait`` set, the call long-polls — it parks on the server until a task changes past
        ``since`` (the last version this caller saw) or ``wait`` seconds elapse, then returns the
        current snapshot + version. Without ``wait`` it's an immediate snapshot + version. Feed the
        returned ``version`` back as ``since`` on the next call to wait for the *next* change —
        replacing a ``list_tasks()`` + ``sleep`` poll loop with one event-driven call.
        """
        params: dict[str, Any] = {"since": since}
        if wait is None:
            resp = self._http.get("/tasks", params=params)
        else:
            # Give the socket headroom past the server-side hold so the long-poll isn't cut short
            # by httpx's default read timeout.
            params["wait"] = wait
            resp = self._http.get("/tasks", params=params, timeout=httpx.Timeout(wait + 10.0))
        resp.raise_for_status()
        version = int(resp.headers.get("X-Tasks-Version", "0"))
        return cast("list[JsonObj]", resp.json()), version

    def get_task(self, task_id: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.get(f"/tasks/{task_id}")))

    def list_transitions(self, task_id: str) -> list[str]:
        return cast("list[str]", self._json(self._http.get(f"/tasks/{task_id}/transitions")))

    def list_operations(self, task_id: str) -> dict[str, str]:
        return cast("dict[str, str]", self._json(self._http.get(f"/tasks/{task_id}/operations")))

    def list_states(self, task_id: str) -> list[str]:
        """Every state of the task's workflow — the candidates for a free state-set."""
        return cast("list[str]", self._json(self._http.get(f"/tasks/{task_id}/states")))

    def list_skills(self, task_id: str) -> list[JsonObj]:
        """The active workflow's in-container skills (the harness renders these to the CLI)."""
        return cast("list[JsonObj]", self._json(self._http.get(f"/tasks/{task_id}/skills")))

    def get_briefing(self, task_id: str) -> str:
        """The agent's current-phase briefing (the user-prompt hook emits it into context)."""
        body = cast(JsonObj, self._json(self._http.get(f"/tasks/{task_id}/briefing")))
        return cast(str, body["briefing"])

    def workflow_overview(self, task_id: str) -> str:
        """The task's whole-workflow map (the launcher puts it in claude's system prompt)."""
        body = cast(JsonObj, self._json(self._http.get(f"/tasks/{task_id}/workflow-overview")))
        return cast(str, body["overview"])

    def list_registrations(self, task_id: str) -> list[JsonObj]:
        return cast("list[JsonObj]", self._json(self._http.get(f"/tasks/{task_id}/registrations")))

    # -- repos / tasks ------------------------------------------------------------

    def create_repo(
        self,
        repo_id: str,
        name: str,
        git_url: str,
        default_base: str = "main",
        *,
        env_file: str | None = None,
        creds_volume: str | None = None,
    ) -> JsonObj:
        body = {
            "id": repo_id, "name": name, "git_url": git_url, "default_base": default_base,
            "env_file": env_file, "creds_volume": creds_volume,
        }
        return cast(JsonObj, self._json(self._http.post("/repos", json=body)))

    def update_repo(self, repo_id: str, **changes: Any) -> JsonObj:
        """Partially update a repo (PATCH): only the supplied fields are sent, and the service
        merges them onto the stored repo — so untouched fields (e.g. image_layer/capabilities)
        are preserved."""
        return cast(JsonObj, self._json(self._http.patch(f"/repos/{repo_id}", json=changes)))

    def create_task(self, repo_id: str, workflow: str, description: str | None = None) -> JsonObj:
        body = {"repo_id": repo_id, "workflow": workflow, "description": description}
        return cast(JsonObj, self._json(self._http.post("/tasks", json=body)))

    def set_slug(self, task_id: str, slug: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/slug", json={"slug": slug})))

    def set_url(self, task_id: str, url: str) -> JsonObj:
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/url", json={"url": url})))

    def set_tokens_used(self, task_id: str, tokens_used: int) -> JsonObj:
        """Record cumulative tokens used by claude in this container (the Stop hook reports it)."""
        return cast(JsonObj, self._json(self._http.put(
            f"/tasks/{task_id}/tokens-used", json={"tokens_used": tokens_used})))

    def set_state(self, task_id: str, state: str) -> JsonObj:
        """The user's free override — move the task to any state (bypasses the graph and gate)."""
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/state", json={"state": state})))

    def set_turn(self, task_id: str, turn: str) -> JsonObj:
        """Flip who holds the turn (the in-container stop/user-prompt hooks call this)."""
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/turn", json={"turn": turn})))

    def set_blocked(self, task_id: str, blocked: bool) -> JsonObj:
        """Set/clear the deliberate `blocked` marker (survives turn flips)."""
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/blocked", json={"blocked": blocked})))

    def record_provisioning(self, task_id: str, branch: str, clone: str) -> JsonObj:
        """Record the slug-named branch + per-task clone the session service created (ADR 0011)."""
        body: JsonObj = {"branch": branch, "clone": clone}
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/provisioning", json=body)))

    def claim(self, task_id: str, runner_id: str) -> JsonObj:
        """Claim an unclaimed task for `runner_id` (the spawn gate); 409 if another runner holds it."""
        return cast(JsonObj, self._json(self._http.put(f"/tasks/{task_id}/claim", json={"runner_id": runner_id})))

    def release(self, task_id: str) -> JsonObj:
        """Release a task's claim (back to unclaimed) so it can be re-claimed / respawned."""
        return cast(JsonObj, self._json(self._http.delete(f"/tasks/{task_id}/claim")))

    def request_transition(
        self, task_id: str, to_state: str, *, trigger: str | None = None, note: str | None = None
    ) -> JsonObj:
        body: JsonObj = {"to_state": to_state, "trigger": trigger, "note": note}
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/transition", json=body)))

    def apply_operation(self, task_id: str, operation: str) -> JsonObj:
        """Apply a named core operation (e.g. advance/drop); the workflow resolves the target."""
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/operations/{operation}")))

    def resolve_responsibility(
        self, task_id: str, key: str, status: Status, comment: str | None = None
    ) -> JsonObj:
        """Resolve one of the current state's promised responsibilities (MET or FAILED)."""
        body: JsonObj = {"key": key, "status": status.value, "comment": comment}
        return cast(JsonObj, self._json(self._http.post(f"/tasks/{task_id}/responsibilities", json=body)))

    # -- artifacts ----------------------------------------------------------------

    def list_artifacts(self, task_id: str) -> list[str]:
        return cast(list[str], self._json(self._http.get(f"/tasks/{task_id}/artifacts")))

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
