"""Host-side spawn loop (ADR 0008): claim an unclaimed task, then spawn its container.

The session service is the per-host runner. Each pass it considers the tasks the task service knows
that are **unclaimed** and **non-terminal**, **claims** one for this host (the claim is the spawn
gate — exactly one runner owns it; a lost race is a 409 we skip), prepares its writable per-task
clone (`prepare_workspace`), and spawns the container via the runner with the repo's secrets + the
``/workspace`` mount. Provisioning (slug → branch) is the sibling loop (`ProvisionDaemon`); the
unified host daemon runs both. LLM-free.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.models import ContainerStatus, LifecyclePhase
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.executions import WorkflowExecutions
from panopticon.sessionservice.images import ImageBuilder
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.shell_runner import ShellRunner
from panopticon.sessionservice.spawn import cleanup_workspace, prepare_workspace

_log = logging.getLogger(__name__)


def _run_repo_hook(hook_file: str, task_id: str, repo_name: str, workspace: str) -> None:
    """Run a repo's pre-launch hook on the host (blocking). Raises on nonzero exit.

    Runs with ``cwd=workspace`` so relative paths in the hook resolve against the checkout.
    Silently skipped when ``hook_file`` is not present or not executable — lets operators
    register a hook path that doesn't exist yet without breaking spawns.
    """
    if not (os.path.isfile(hook_file) and os.access(hook_file, os.X_OK)):
        return
    result = subprocess.run(
        [hook_file],
        cwd=workspace,
        env={
            **os.environ,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_REPO_NAME": repo_name,
            "PANOPTICON_WORKSPACE": workspace,
        },
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"repo hook {hook_file!r} exited {result.returncode}")


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
        shell_runner: ShellRunner | None = None,
        executions: WorkflowExecutions | None = None,
        git: object | None = None,
        images: ImageBuilder | None = None,
        run_hook: Callable[[str, str, str, str], None] | None = None,
        makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
        exists: Callable[[str], bool] = os.path.isdir,
        rmtree: Callable[[str], None] = shutil.rmtree,
        docker_cleanup: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.monotonic,
        max_respawns: int = MAX_RESPAWNS,
        respawn_reset: float = RESPAWN_RESET_SECONDS,
    ) -> None:
        self._client = client
        self._runner = runner
        self._shell_runner = shell_runner
        self._runner_id = runner_id
        #: The cached "how is this workflow run" lookup (runner_type + shell details), shared with the
        #: provisioner so both agree on which tasks are shell. The per-pass calls (reconcile/cleanup/
        #: heal) go through it, so it must not re-hit the service each time.
        self._executions = executions or WorkflowExecutions(client)
        self._cache = cache
        self._tasks_root = tasks_root
        self._git = git
        self._images = images or ImageBuilder()
        self._run_hook = run_hook or _run_repo_hook
        self._makedirs = makedirs
        self._exists = exists
        self._rmtree = rmtree
        self._docker_cleanup = (
            docker_cleanup if docker_cleanup is not None else runner.delete_workspace_contents
        )
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
        """Spawn the execution backend for an **already claimed** task — the body shared by
        :meth:`spawn_one` (after it wins the claim) and :meth:`heal` (respawning an orphan this
        runner already holds). Routes on the workflow's ``runner_type``: a ``"shell"`` workflow runs
        its script in a host tmux session (no clone, no image); otherwise the Docker container path.

        Reports each phase (``CLAIMING`` → … → ``AWAITING``); a step raising is reported as
        ``FAILED`` (with the error) before re-raising, so the host daemon's per-task isolation still
        applies but the failure is visible, not silent."""
        task_id = task["id"]
        try:
            _log.info("task %s: claiming", task_id)
            self._report(task_id, LifecyclePhase.CLAIMING)
            repo = self._client.get_repo(task["repo_id"])
            if self._executions.is_shell(task["workflow"]):
                return self._spawn_shell(task, repo)
            return self._spawn_container(task, repo)
        except Exception as exc:
            self._report(task_id, LifecyclePhase.FAILED, detail=str(exc))
            raise

    def _prepare_task_dir(self, task: JsonObj, repo: JsonObj, *, clone: bool) -> str:
        """The task's working directory (``<tasks_root>/<task_id>``) — shared by both backends.

        With ``clone`` it's a per-task ``git clone --local`` of the repo with ``origin`` pointed at
        the forge (:func:`prepare_workspace`, idempotent); otherwise just an empty directory. Reports
        ``PREPARING`` either way (it's the "readying the workspace" step). Returns the path."""
        task_id = task["id"]
        _log.info(
            "task %s: preparing workspace (repo=%s, clone=%s)",
            task_id,
            repo.get("name", repo["id"]),
            clone,
        )
        self._report(task_id, LifecyclePhase.PREPARING)
        if clone:
            return prepare_workspace(
                task_id,
                repo,
                cache=self._cache,
                tasks_root=self._tasks_root,
                git=self._git,  # type: ignore[arg-type]
                makedirs=self._makedirs,
            )
        workdir = f"{self._tasks_root}/{task_id}"
        self._makedirs(workdir)
        return workdir

    def _spawn_container(self, task: JsonObj, repo: JsonObj) -> str:
        """The Docker path: clone the per-task workspace, compose base → workflow → repo, and spawn
        the container (reports ``PREPARING`` → ``BUILDING`` → ``STARTING`` → ``AWAITING``)."""
        task_id = task["id"]
        workspace = self._prepare_task_dir(
            task, repo, clone=True
        )  # a container always mounts a checkout
        if hook_file := repo.get("hook_file"):
            self._run_hook(hook_file, task_id, repo["name"], workspace)
        _log.info(
            "task %s: building image (workflow=%s, repo=%s)",
            task_id,
            task["workflow"],
            repo.get("name", repo["id"]),
        )
        self._report(task_id, LifecyclePhase.BUILDING)
        self._images.build_base_if_missing(verbose=True)
        image = self._compose_image(task["workflow"], repo)
        return self._runner.spawn(
            task_id,
            env_file=repo.get("env_file"),
            workspace=workspace,
            image=image,
            docker_in_docker=bool((repo.get("capabilities") or {}).get("docker_in_docker")),
            initial_prompt=task.get(
                "initial_prompt"
            ),  # passed as a CLI arg to claude on the first run
            turn=task.get("turn"),  # agent's turn → INTERRUPT_PROMPT on respawn
            starting_model=task.get(
                "starting_model"
            ),  # model selection passed to claude --model on first launch
            progress=lambda phase: self._report(task_id, phase),  # STARTING then AWAITING
        )

    def _spawn_shell(self, task: JsonObj, repo: JsonObj) -> str:
        """The shell path: run the workflow's ``shell_script`` in a host tmux session — no image, no
        agent (reports ``PREPARING`` → ``STARTING`` → ``AWAITING``, skipping ``BUILDING``).

        Shares the same task directory as a container task (:meth:`_prepare_task_dir`): empty by
        default, or a repo clone when the workflow sets ``clone_repo``. The script starts there unless
        the workflow overrides it with an explicit ``shell_workdir``. Cleaned up with the rest when the
        task finishes (:meth:`cleanup`)."""
        task_id = task["id"]
        if self._shell_runner is None:
            raise RuntimeError(
                f"task {task_id!r} uses shell workflow {task['workflow']!r} but this runner has no shell runner"
            )
        spec = self._executions.spec(task["workflow"])
        task_dir = self._prepare_task_dir(task, repo, clone=bool(spec["clone_repo"]))
        workdir = spec["workdir"] or task_dir  # the workflow's override, else the task's own dir
        _log.info(
            "task %s: starting shell session (workflow=%s, workdir=%s)",
            task_id,
            task["workflow"],
            workdir,
        )
        return self._shell_runner.spawn(
            task_id,
            env_file=repo.get("env_file"),  # per-repo secrets, sourced into the shell (ADR 0007)
            script=spec["script"],
            workdir=workdir,
            progress=lambda phase: self._report(task_id, phase),  # STARTING then AWAITING
        )

    def _runner_for(self, task: JsonObj) -> LocalRunner | ShellRunner:
        """The runner that owns ``task``'s session — the shell runner for a shell workflow (when one
        is configured), else the Docker runner. Lets the liveness probes (``is_running`` /
        ``has_session``) in :meth:`reconcile`, :meth:`cleanup`, :meth:`startup_reclaim` and
        :meth:`_is_orphan` check the right backend. Tasks without a ``workflow`` key (some internal
        callers) fall back to the Docker runner."""
        if self._shell_runner is not None and self._executions.is_shell(task.get("workflow")):
            return self._shell_runner
        return self._runner

    def _report(self, task_id: str, phase: LifecyclePhase, detail: str | None = None) -> None:
        """Push a spawn phase for ``task_id`` to the task service (best-effort: a reporting blip must
        not abort the spawn, so failures are swallowed)."""
        if phase == LifecyclePhase.STARTING:
            _log.info("task %s: starting container", task_id)
        elif phase == LifecyclePhase.AWAITING:
            _log.info("task %s: awaiting registration", task_id)
        elif phase == LifecyclePhase.FAILED:
            _log.error("task %s: spawn failed — %s", task_id, detail)
        with contextlib.suppress(httpx.HTTPError):
            self._client.report_lifecycle(task_id, self._runner_id, phase.value, detail)

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
        if self._runner_for(task).is_running(task["id"]):
            return  # container/session present, just not registered yet — still coming up
        self._client.clear_lifecycle(task["id"])  # container gone → composes `down`

    def _is_orphan(self, task: JsonObj) -> bool:
        """Whether ``task`` is an orphan **this** runner should self-heal: claimed by us,
        non-terminal, and with no tmux session (the make-stop case — see :meth:`heal`). The
        session probe is last so the cheap claim/terminal checks short-circuit it.

        Shell tasks are never orphans: their script exiting is natural completion (or an
        operator cancelling), not a crash to respawn — so re-running it would be wrong."""
        if task.get("claimed_by") != self._runner_id or task["state"] in TERMINAL_LABELS:
            return False
        if self._executions.is_shell(task.get("workflow")):
            return False
        return not self._runner.has_session(task["id"])

    def _respawn_count(self, task_id: str, now: float) -> int:
        """The task's respawn count for the crash-loop guard, with the survivor-window reset applied
        (a respawn that survived :data:`RESPAWN_RESET_SECONDS` starts a fresh budget). Read-only —
        :meth:`heal` is what records a respawn."""
        count, last = self._respawns.get(task_id, (0, 0.0))
        if count and now - last >= self._respawn_reset:
            return 0  # survived long enough since the last respawn → a fresh episode, not a loop
        return count

    def mark_healing(self, task: JsonObj) -> None:
        """Flag an orphan as ``HEALING`` up front, before the serial respawn (:meth:`heal`) reaches
        it — a cheap REST-only report with no spawn.

        Respawning is serial (each :meth:`heal` blocks on ``docker run`` + the tmux session), so
        marking in the respawn itself would light up only the orphan currently coming back, leaving
        the queued ones reading ``down`` — indistinguishable from a dead task. The host daemon runs
        this over every task *before* the respawn loop, so all orphans show ``healing`` at once and
        each clears to ``live`` as its respawn completes. Idempotent: skips a task already flagged
        (so it doesn't churn the change feed) and one that's exhausted its respawn budget (we've
        stopped respawning it — let it read ``down``/``failed``, not a perpetual ``healing``)."""
        if not self._is_orphan(task):
            return
        if self._respawn_count(task["id"], self._now()) >= self._max_respawns:
            return  # crash-looped out — not actually healing it any more
        if task.get("container_status") == ContainerStatus.HEALING.value:
            return  # already flagged — re-reporting would only churn the change feed
        self._report(task["id"], LifecyclePhase.HEALING)

    def heal(self, task: JsonObj) -> str | None:
        """Self-heal an orphaned task: respawn one this runner claims whose tmux session is gone.

        A kill of the ``-L panopticon`` tmux server that *isn't* ``make stop`` — a crash, a manual
        ``tmux kill-server``, a single killed session — destroys the task session but leaves its
        **detached container running and heartbeating**, so the task reads ``live`` yet has no session
        to attach to, and the unclaimed-gated spawn loop won't recover it (it's still claimed by this
        runner). ``make stop`` itself now stops the task containers too, but the task stays claimed, so
        the same gap remains: a recovery path that doesn't depend on the container being unclaimed.
        Here we close it: a task claimed by **this** runner, non-terminal, with **no tmux session** is
        respawned via the idempotent spawn path (:meth:`_spawn` → the runner ``docker rm --force``s any
        stale container and starts a fresh session; the agent resumes from the per-task config volume
        via ``claude --continue``).
        :meth:`mark_healing` flags such an orphan ``HEALING`` before this serial respawn reaches it.

        Gating on session-existence distinguishes the two restart cases with no extra state: a server
        teardown (``make stop`` or a crash) leaves all sessions gone → every orphan is respawned; a
        runner-process-only restart leaves the sessions (and their agents) alive → they're skipped,
        untouched.

        A crash-loop guard caps consecutive respawns (:data:`MAX_RESPAWNS`) so a container that won't
        stay up is logged and left for attention rather than thrashed; a respawn that survives
        :data:`RESPAWN_RESET_SECONDS` resets the budget. Returns the new container id, or ``None`` when
        nothing was done. Per-tick call from the host daemon, so it runs on start and continuously."""
        task_id = task["id"]
        if task["state"] in TERMINAL_LABELS and task.get("claimed_by") == self._runner_id:
            self._respawns.pop(task_id, None)  # our task is done — forget any crash-loop tracking
        if not self._is_orphan(task):
            return None  # not ours / terminal / a session is up — nothing to heal
        now = self._now()
        count = self._respawn_count(task_id, now)
        if count >= self._max_respawns:
            _log.error(
                "task %s keeps losing its tmux session (%d respawns) — leaving it for attention",
                task_id,
                count,
            )
            return None
        self._respawns[task_id] = (count + 1, now)
        _log.warning(
            "self-healing orphaned task %s (no tmux session) — respawn %d", task_id, count + 1
        )
        return self._spawn(task)

    def startup_reclaim(self, tasks: list[JsonObj]) -> None:
        """Release claims for our tasks whose containers aren't running — the restart reset.

        On every restart (clean ``make stop`` or unexpected reboot), tasks stay
        ``claimed_by`` this runner in the DB but their containers are gone. Without this,
        :meth:`_is_orphan` fires immediately and :meth:`heal` respawns so fast the
        dashboard sees ``live`` with no visible lifecycle phases.

        :meth:`~panopticon.sessionservice.local_runner.LocalRunner.is_running` distinguishes
        the two cases: if the container is gone (reboot / clean stop), the claim is released
        so the task reads ``queued`` and goes through the full visible spawn lifecycle; if it
        survived (runner-only crash, not a full restart), the claim is kept and :meth:`heal`
        handles it normally.

        Best-effort per task: a failed release is silently skipped — :meth:`heal` picks it
        up on the next tick, fast but functional. Called once by
        :meth:`~panopticon.sessionservice.host.HostDaemon.run` on the first successful task
        fetch.

        Shell tasks are left claimed: releasing one would let :meth:`spawn_one` re-run its script
        (the unclaimed-spawn path), but a shell script is run **once** — its exit is completion, not
        a crash to recover (the same reason :meth:`_is_orphan`/:meth:`heal` skip them). A shell task
        whose session is gone reconciles to ``down`` for the operator to drop or respawn explicitly."""
        for task in tasks:
            if task.get("claimed_by") != self._runner_id:
                continue
            if task["state"] in TERMINAL_LABELS:
                continue
            if self._executions.is_shell(task.get("workflow")):
                continue  # never auto-respawn a shell task — leave it claimed (reconciles to `down`)
            if self._runner_for(task).is_running(task["id"]):
                continue  # container survived (runner-only crash) — keep claim, heal handles it
            # best-effort — heal() picks up unclaimed tasks that failed to release
            with contextlib.suppress(httpx.HTTPError):
                self._client.release(task["id"])

    def cleanup(self, task: JsonObj) -> None:
        """Remove the per-task workspace once a terminal task's container has exited.

        Self-gates on two conditions so calling this on every task each pass is safe:
        the task must be terminal (COMPLETE/DROPPED) **and** the container must no longer
        be running — so we never delete a workspace while the agent is still active, and
        we don't need to force-stop anything."""
        if task["state"] not in TERMINAL_LABELS:
            return
        if self._runner_for(task).is_running(task["id"]):
            return  # container/session still up — wait for it to exit naturally
        cleanup_workspace(
            task["id"],
            self._tasks_root,
            exists=self._exists,
            rmtree=self._rmtree,
            docker_cleanup=self._docker_cleanup,
        )

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
        return self._images.build(workflow, repo["id"], layers, verbose=True)


def spawnable_tasks(client: TaskServiceClient) -> Callable[[], list[JsonObj]]:
    """This host's spawn candidates: unclaimed, non-terminal tasks (the runner claims-then-spawns).

    For M1 (single host) that's every such task the service knows; scoping to this runner's own
    assignments is an M5 refinement.
    """
    return lambda: [
        t for t in client.list_tasks() if not t["claimed_by"] and t["state"] not in TERMINAL_LABELS
    ]
