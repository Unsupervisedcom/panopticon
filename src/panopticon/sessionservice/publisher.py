"""Host-side publishing: push a task's merge back to ``origin`` (the local-git flow).

The sibling of :mod:`~panopticon.sessionservice.provisioner`, and the same split: the session
service runs **where the per-task clone lives**, so it owns the host git; the task service only
records the result.

Why the host and not the container: a per-task clone's ``origin`` is the repo's ``git_url``
verbatim, and for a local repo that's a path on the *host* (``/home/me/src/thing``). Only the
clone itself is mounted into the container, so that path doesn't exist there — an in-container
``git push origin main`` fails before it starts. The agent therefore *asks* for the push
(``request_push``) and this performs it, reporting the outcome back
(``record_push``). LLM-free: pure git + REST.

Two refs go, **in this order**:

1. the task branch (``panopticon/<slug>``) — a backup that lands even when the base branch won't,
   because it is never the branch the destination repo has checked out;
2. the base branch the agent merged into.

That ordering is the point. A non-bare repo refuses a push to its checked-out branch by default
(``receive.denyCurrentBranch``), so the base push is the one that can fail — and when it does, the
task's commits are already in the operator's repo on the task branch rather than stranded in a
throwaway clone. Hence three outcomes, not two: ``PUSHED``, ``PARTIAL`` (backup only), ``FAILED``.
"""

from __future__ import annotations

import logging

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.git import GitClones, GitError
from panopticon.core.models import PushStatus
from panopticon.sessionservice.executions import WorkflowExecutions

_log = logging.getLogger(__name__)

#: Longest ``stderr`` excerpt carried into a recorded ``detail`` when nothing more specific was
#: recognized — enough for the operator to see git's own words without pasting a wall into the task.
_STDERR_EXCERPT = 400


def _excerpt(stderr: str) -> str:
    """Git's complaint, whitespace-collapsed and capped — the fallback when it isn't classified."""
    text = " ".join(stderr.split())
    return text if len(text) <= _STDERR_EXCERPT else f"{text[:_STDERR_EXCERPT]}…"


def explain_push_failure(stderr: str, *, branch: str, task_branch: str | None) -> str:
    """Turn git's rejection into something the operator can act on.

    Every branch here ends in a concrete next step, because this string is what the agent reads
    back to the user — a bare ``! [remote rejected]`` would leave them to diagnose it themselves.
    """
    lowered = stderr.lower()
    fallback = (
        f" Your commits are safe on {task_branch} in the repo — `git merge {task_branch}` lands "
        "them by hand."
        if task_branch
        else ""
    )
    if "denycurrentbranch" in lowered or "checked out branch" in lowered:
        return (
            f"origin has {branch} checked out, and git refuses to push to a checked-out branch "
            "(a push moves the branch pointer without updating the files, which would leave that "
            "repo reporting the new commits as uncommitted changes reverting them). Run `git "
            "config receive.denyCurrentBranch updateInstead` there to accept pushes and update "
            f"the files with them.{fallback}"
        )
    if "uncommitted changes" in lowered or "unstaged changes" in lowered or "clobber" in lowered:
        return (
            f"origin accepts pushes to {branch} but its working tree has uncommitted changes, so "
            f"git would not update the files. Commit or stash them there, then retry.{fallback}"
        )
    if "non-fast-forward" in lowered or "fetch first" in lowered or "behind its remote" in lowered:
        return (
            f"origin's {branch} has moved on since this task branched, so the push is not a "
            "fast-forward. Fetch origin and merge it in before pushing again."
        )
    if (
        "could not read username" in lowered
        or "authentication failed" in lowered
        or "permission denied" in lowered
        or "does not appear to be a git repository" in lowered
    ):
        return (
            "the session service could not reach or authenticate to origin. It holds no "
            "credentials for a networked remote — this workflow is meant for repos whose origin "
            f"is a local path.{fallback}"
        )
    return f"pushing {branch} to origin failed: {_excerpt(stderr)}{fallback}"


class Publisher:
    """Performs each task's requested push and records how it went.

    ``clones_root`` holds the per-task clones (``<clones_root>/<task_id>``), the same layout
    :class:`~panopticon.sessionservice.provisioner.Provisioner` branches. ``git`` is injectable so
    the emitted commands are unit-testable without a real repo; ``executions`` is the shared
    "how is this workflow run" cache, so shell tasks (no clone at all) are skipped the same way.
    """

    def __init__(
        self,
        client: TaskServiceClient,
        *,
        clones_root: str,
        git: GitClones | None = None,
        executions: WorkflowExecutions | None = None,
        remote: str = "origin",
    ) -> None:
        self._client = client
        self._clones_root = clones_root.rstrip("/")
        self._git = git or GitClones()
        self._executions = executions or WorkflowExecutions(client)
        self._remote = remote

    def publish(self, task: JsonObj) -> PushStatus | None:
        """Push ``task``'s merge if one is pending, returning the recorded status (else ``None``).

        Pending means the task carries a push record still in ``REQUESTED``; anything else no-ops,
        so the host loop can call this on every task each pass. Recording the outcome is what
        clears the gate, which makes a second pass a no-op even though the push itself isn't
        idempotent in any deeper sense.
        """
        push = task.get("push")
        if not push or push.get("status") != PushStatus.REQUESTED.value:
            return None
        if self._executions.is_shell(task.get("workflow")):
            return None  # runs on the host with no per-task clone — nothing to push from
        task_id = task["id"]
        clone = task.get("clone") or f"{self._clones_root}/{task_id}"
        base = str(push["branch"])
        task_branch = task.get("branch")
        # The backup goes first: it lands even when the base branch is refused, so the work
        # reaches the operator's repo either way.
        backup_pushed, backup_detail = True, ""
        if task_branch and task_branch != base:
            try:
                self._git.push(repo_path=clone, remote=self._remote, branch=str(task_branch))
            except GitError as err:
                backup_pushed, backup_detail = False, _excerpt(err.stderr)
                _log.warning("task %s: pushing backup branch %s failed", task_id, task_branch)
        try:
            self._git.push(repo_path=clone, remote=self._remote, branch=base)
        except GitError as err:
            reason = explain_push_failure(
                err.stderr, branch=base, task_branch=str(task_branch) if backup_pushed else None
            )
            if backup_pushed and task_branch:
                return self._record(task_id, PushStatus.PARTIAL, reason)
            if not backup_pushed:
                reason = f"{reason} The task branch could not be pushed either: {backup_detail}"
            return self._record(task_id, PushStatus.FAILED, reason)
        pushed = f"pushed {base} to {self._remote}"
        if task_branch and task_branch != base:
            pushed += f" (and {task_branch})" if backup_pushed else f" ({task_branch} failed)"
        return self._record(task_id, PushStatus.PUSHED, pushed)

    def _record(self, task_id: str, status: PushStatus, detail: str) -> PushStatus:
        self._client.record_push(task_id, status.value, detail)
        _log.info("task %s: push %s — %s", task_id, status.value, detail)
        return status
