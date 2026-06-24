"""Host-side spawn loop (ADR 0008): claim an unclaimed task, then spawn its container.

The session service is the per-host runner. Each pass it considers the tasks the task service knows
that are **unclaimed** and **non-terminal**, **claims** one for this host (the claim is the spawn
gate — exactly one runner owns it; a lost race is a 409 we skip), prepares its writable per-task
clone (`prepare_workspace`), and spawns the container via the runner with the repo's secrets + the
``/workspace`` mount. Provisioning (slug → branch) is the sibling loop (`ProvisionDaemon`); the
unified host daemon runs both. LLM-free.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.models import ContainerStatus, LifecyclePhase
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.images import ImageBuilder
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawn import prepare_workspace

#: Statuses that mean "a spawn this runner reported is still in flight" — the ones :meth:`reconcile`
#: acts on. A claimed-by-us task stuck in one of these whose container isn't running is **down**.
_IN_PROGRESS = frozenset(
    s.value
    for s in (
        ContainerStatus.CLAIMING,
        ContainerStatus.PREPARING,
        ContainerStatus.BUILDING,
        ContainerStatus.STARTING,
        ContainerStatus.AWAITING,
    )
)


class Spawner:
    """Claims an unclaimed task for ``runner_id`` and spawns its container (the per-host runner)."""

    def __init__(
        self,
        client: TaskServiceClient,
        runner: LocalRunner,
        *,
        runner_id: str,
        cache: CloneCache,
        tasks_root: str,
        git: object | None = None,
        images: ImageBuilder | None = None,
    ) -> None:
        self._client = client
        self._runner = runner
        self._runner_id = runner_id
        self._cache = cache
        self._tasks_root = tasks_root
        self._git = git
        self._images = images or ImageBuilder()

    def spawn_one(self, task: JsonObj) -> str | None:
        """Claim + spawn ``task`` if it's a fresh unclaimed, non-terminal task; else ``None``.

        Claiming is compare-and-set on the task service — if another runner wins it (409) we skip.
        On a successful claim, prepare the per-task clone and spawn the container with the repo's
        secrets + the ``/workspace`` mount; returns the container id.

        Reports each spawn phase to the task service as it goes (``CLAIMING`` → ``PREPARING`` →
        ``BUILDING`` → ``STARTING`` → ``AWAITING``) so the dashboard can surface the steps to becoming
        live; a step raising is reported as ``FAILED`` (with the error) before re-raising, so the
        host daemon's per-task isolation still applies but the failure is visible, not silent."""
        if task["state"] in TERMINAL_LABELS or task.get("claimed_by"):
            return None
        try:
            self._client.claim(task["id"], self._runner_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return None  # another runner claimed it first
            raise
        task_id = task["id"]
        try:
            self._report(task_id, LifecyclePhase.CLAIMING)
            repo = self._client.get_repo(task["repo_id"])
            self._report(task_id, LifecyclePhase.PREPARING)
            workspace = prepare_workspace(
                task_id, repo, cache=self._cache, tasks_root=self._tasks_root, git=self._git  # type: ignore[arg-type]
            )
            self._report(task_id, LifecyclePhase.BUILDING)
            image = self._compose_image(task["workflow"], repo)
            return self._runner.spawn(
                task_id,
                env_file=repo.get("env_file"),
                creds_volume=repo.get("creds_volume"),
                workspace=workspace,
                image=image,
                docker_in_docker=bool((repo.get("capabilities") or {}).get("docker_in_docker")),
                memo=task.get("memo"),  # pre-filled into claude's input box on first spawn
                progress=lambda phase: self._report(task_id, phase),  # STARTING then AWAITING
            )
        except Exception as exc:
            self._report(task_id, LifecyclePhase.FAILED, detail=str(exc))
            raise

    def _report(self, task_id: str, phase: LifecyclePhase, detail: str | None = None) -> None:
        """Push a spawn phase for ``task_id`` to the task service (best-effort: a reporting blip must
        not abort the spawn, so failures are swallowed)."""
        try:
            self._client.report_lifecycle(task_id, self._runner_id, phase.value, detail)
        except httpx.HTTPError:
            pass

    def reconcile(self, task: JsonObj) -> None:
        """Reconcile a task this runner claims into the right lifecycle status (down-detection).

        For a task claimed by **this** runner whose reported spawn is still in flight (``CLAIMING`` …
        ``AWAITING``) but whose container isn't actually running, clear the stale phase so the task
        service composes ``down`` — the authoritative replacement for the dashboard's old guess. A
        registered (``live``) or already-``down``/``failed`` task is left alone; an in-flight one
        whose container *is* still running is left to keep coming up."""
        if task.get("claimed_by") != self._runner_id:
            return  # not ours (or unclaimed) — spawn_one handles the unclaimed case
        if task.get("container_status") not in _IN_PROGRESS:
            return  # live / down / failed / queued / disconnected — nothing to reconcile
        if self._runner.is_running(task["id"]):
            return  # container present, just not registered yet — still coming up
        self._client.clear_lifecycle(task["id"])  # container gone → composes `down`

    def _compose_image(self, workflow: str, repo: JsonObj) -> str | None:
        """Compose the task's image (base → workflow → repo layers, ADR 0005) and return its tag;
        ``None`` when neither tier contributes a layer (the runner falls back to the base image).
        E.g. github-peer-reviewed layers `gh` for its forge skills, then the repo layers its toolchain (`uv`,
        `make`). Docker layer-caches, so this is a no-op once built."""
        layers = [self._client.workflow_image_layer(workflow), repo.get("image_layer") or ""]
        layers = [layer for layer in layers if layer.strip()]
        if not layers:
            return None
        return self._images.build(workflow, repo["id"], layers)


def spawnable_tasks(client: TaskServiceClient) -> Callable[[], list[JsonObj]]:
    """This host's spawn candidates: unclaimed, non-terminal tasks (the runner claims-then-spawns).

    For M1 (single host) that's every such task the service knows; scoping to this runner's own
    assignments is an M5 refinement.
    """
    return lambda: [
        t for t in client.list_tasks() if not t["claimed_by"] and t["state"] not in TERMINAL_LABELS
    ]
