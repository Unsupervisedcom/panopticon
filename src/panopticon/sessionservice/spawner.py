"""Host-side spawn loop (ADR 0008): claim an unclaimed task, then spawn its container.

The session service is the per-host runner. Each pass it considers the tasks the task service knows
that are **unclaimed** and **non-terminal**, **claims** one for this host (the claim is the spawn
gate — exactly one runner owns it; a lost race is a 409 we skip), prepares its writable per-task
clone (`prepare_workspace`), and spawns the container via the runner with the repo's secrets + the
``/workspace`` mount. Provisioning (slug → branch) is the sibling loop (`ProvisionDaemon`); the
unified host daemon runs both. LLM-free.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.models import ContainerStatus, LifecyclePhase
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.images import ImageBuilder
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawn import prepare_workspace

_log = logging.getLogger(__name__)

#: Crash-loop guard for :meth:`Spawner.heal`: at most this many respawns of a task within a burst
#: before we stop and surface it (log) rather than thrash a container that won't stay up.
MAX_RESPAWNS = 5
#: A respawn that survives this long counts as recovered — the next time the task loses its session
#: starts a fresh respawn budget instead of counting toward the previous burst. So an isolated
#: orphan heals every time, while a tight crash loop exhausts the budget and is left for attention.
RESPAWN_RESET_SECONDS = 60.0

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
        now: Callable[[], float] = time.monotonic,
        max_respawns: int = MAX_RESPAWNS,
        respawn_reset: float = RESPAWN_RESET_SECONDS,
    ) -> None:
        self._client = client
        self._runner = runner
        self._runner_id = runner_id
        self._cache = cache
        self._tasks_root = tasks_root
        self._git = git
        self._images = images or ImageBuilder()
        self._now = now
        self._max_respawns = max_respawns
        self._respawn_reset = respawn_reset
        #: task_id → (respawns in the current burst, monotonic time of the last respawn), the
        #: crash-loop guard state for :meth:`heal`.
        self._respawns: dict[str, tuple[int, float]] = {}

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
        return self._spawn(task)

    def _spawn(self, task: JsonObj) -> str:
        """Prepare the workspace, compose the image, and spawn the container for an **already
        claimed** task — the body shared by :meth:`spawn_one` (after it wins the claim) and
        :meth:`heal` (respawning an orphan this runner already holds).

        Reports each phase (``CLAIMING`` → … → ``AWAITING``); a step raising is reported as
        ``FAILED`` (with the error) before re-raising, so the host daemon's per-task isolation still
        applies but the failure is visible, not silent."""
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

    def heal(self, task: JsonObj) -> str | None:
        """Self-heal an orphaned task: respawn one this runner claims whose tmux session is gone.

        A ``make stop`` (or any kill of the ``-L panopticon`` tmux server) destroys every task
        session, but the **detached containers keep running and heartbeating** — so those tasks read
        ``live`` yet have no session to attach to, and the unclaimed-gated spawn loop won't recover
        them (they're still claimed by this runner). Here we close that gap: a task claimed by **this**
        runner, non-terminal, with **no tmux session** is respawned via the idempotent spawn path
        (:meth:`_spawn` → the runner ``docker rm --force``s the stale container and starts a fresh
        session; the agent resumes from the per-task config volume via ``claude --continue``).

        Gating on session-existence distinguishes the two restart cases with no extra state: a full
        ``make stop`` leaves all sessions gone → every orphan is respawned; a runner-process-only
        restart leaves the sessions (and their agents) alive → they're skipped, untouched.

        A crash-loop guard caps consecutive respawns (:data:`MAX_RESPAWNS`) so a container that won't
        stay up is logged and left for attention rather than thrashed; a respawn that survives
        :data:`RESPAWN_RESET_SECONDS` resets the budget. Returns the new container id, or ``None`` when
        nothing was done. Per-tick call from the host daemon, so it runs on start and continuously."""
        task_id = task["id"]
        if task.get("claimed_by") != self._runner_id:
            return None  # not ours (or unclaimed) — spawn_one handles the unclaimed case
        if task["state"] in TERMINAL_LABELS:
            self._respawns.pop(task_id, None)  # done — forget any crash-loop tracking
            return None
        if self._runner.has_session(task_id):
            return None  # a session is up — reachable, nothing to heal
        count, last = self._respawns.get(task_id, (0, 0.0))
        now = self._now()
        if count and now - last >= self._respawn_reset:
            count = 0  # survived long enough since the last respawn → a fresh episode, not a loop
        if count >= self._max_respawns:
            _log.error(
                "task %s keeps losing its tmux session (%d respawns) — leaving it for attention",
                task_id, count,
            )
            return None
        self._respawns[task_id] = (count + 1, now)
        _log.warning("self-healing orphaned task %s (no tmux session) — respawn %d", task_id, count + 1)
        return self._spawn(task)

    def _compose_image(self, workflow: str, repo: JsonObj) -> str | None:
        """Compose the task's image (base → workflow → repo layers, ADR 0005) and return its tag;
        ``None`` when neither tier contributes a layer (the runner falls back to the base image).
        E.g. github-peer-reviewed layers `gh` for its forge skills, then the repo layers its toolchain (`uv`,
        `make`). Docker layer-caches, so this is a no-op once built."""
        layers = [
            self._client.workflow_image_layer(workflow),
            self._client.repo_image_layer(repo["id"]),
        ]
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
