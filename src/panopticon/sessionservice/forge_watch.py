"""Deterministically drive forge-watched workflows through GitHub's ``gh`` CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.dirs import secrets_file_path
from panopticon.core.models import Status
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.executions import WorkflowExecutions
from panopticon.workflows.resident_agent import ResidentAgent, issue_title_and_body

_log = logging.getLogger(__name__)
_GITHUB_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)
_ISSUE_URL = re.compile(r"/issues/(\d+)(?:/)?$")
_PR_URL = re.compile(r"/pull/(\d+)(?:/)?$")


class GhRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> str: ...


def subprocess_run(
    args: Sequence[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> str:
    """Run ``gh`` with captured text output and the caller's narrowly supplied environment."""
    return subprocess.run(
        list(args),
        check=check,
        input=input,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    ).stdout


def gh_argv(gh: str, repo_slug: str, *args: str) -> list[str]:
    """Build a ``gh`` argv that always selects the repository explicitly."""
    split = min(2, len(args))
    return [gh, *args[:split], "--repo", repo_slug, *args[split:]]


def github_repo_slug(git_url: str) -> str:
    """Return ``owner/name`` for the supported GitHub HTTPS and SSH remote forms."""
    match = _GITHUB_URL.fullmatch(git_url.strip())
    if match is None:
        raise ValueError(f"not a supported GitHub repository URL: {git_url!r}")
    return f"{match.group(1)}/{match.group(2)}"


def _env_values(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        parsed = shlex.split(raw_value, comments=True, posix=True)
        values[key.strip()] = " ".join(parsed)
    return values


def _number(url: str | None, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(url or "")
    return int(match.group(1)) if match else None


class ForgeWatcher:
    """Poll claimed forge tasks and apply one idempotent workflow step per task per interval."""

    def __init__(
        self,
        client: TaskServiceClient,
        executions: WorkflowExecutions,
        *,
        runner_id: str,
        run: GhRunner = subprocess_run,
        gh: str = "gh",
        secrets_dir: str | Path | None = None,
        now: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._executions = executions
        self._runner_id = runner_id
        self._run = run
        self._gh = gh
        self._secrets_dir = secrets_dir
        self._now = now
        self._wall_clock = wall_clock
        self._next_poll: dict[str, float] = {}
        self._blocked_sent: set[str] = set()

    def spawn(self, task_id: str) -> str:
        raise NotImplementedError("forge tasks are claimed and watched; they are never spawned")

    def stop(self, task_id: str) -> None:
        """There is no local execution resource to stop."""

    def attach_command(self, task_id: str) -> None:
        raise NotImplementedError("forge-watched tasks have no tmux session to attach")

    def is_running(self, task_id: str) -> bool:
        try:
            task = self._client.get_task(task_id)
        except Exception:
            return False
        return task.get("claimed_by") == self._runner_id

    def has_session(self, task_id: str) -> bool:
        return self.is_running(task_id)

    def watch(self, task: JsonObj) -> None:
        task_id = str(task.get("id", ""))
        workflow = task.get("workflow")
        if (
            not task_id
            or task.get("claimed_by") != self._runner_id
            or not self._executions.is_forge(workflow)
            or task.get("state") in TERMINAL_LABELS
        ):
            return
        now = float(self._now())
        if now < self._next_poll.get(task_id, 0.0):
            return
        interval = self._executions.spec(str(workflow)).get("poll_interval_seconds") or 60
        self._next_poll[task_id] = now + float(interval)
        try:
            repo = self._client.get_repo(task["repo_id"])
            env = self._gh_env(task, repo)
            if env is None:
                return
            slug = github_repo_slug(str(repo["git_url"]))
            state = str(task["state"])
            if state == "FILING":
                self._filing(task, repo, slug, env)
            elif state == "IMPLEMENTING":
                self._implementing(task, slug, env)
            elif state == "REVIEW":
                self._review(task, slug, env)
            elif state == "MERGING":
                self._merging(task, slug, env)
        except Exception:
            _log.warning("task %s: forge watch failed; retrying next poll", task_id, exc_info=True)

    def _gh_env(self, task: JsonObj, repo: JsonObj) -> dict[str, str] | None:
        try:
            path = secrets_file_path(repo.get("env_file"), secrets_dir=self._secrets_dir)
            token = _env_values(path).get("GH_TOKEN") if path else None
        except (OSError, ValueError):
            token = None
        if not token:
            self._block_once(task, "missing repo env_file or GH_TOKEN; forge watch paused")
            return None
        return {"GH_TOKEN": token, "GH_PROMPT_DISABLED": "1"}

    def _call(
        self,
        slug: str,
        env: Mapping[str, str],
        *args: str,
        input: str | None = None,
    ) -> str:
        return self._run(gh_argv(self._gh, slug, *args), env=env, input=input)

    def _block_once(self, task: JsonObj, reason: str) -> None:
        task_id = str(task["id"])
        if task_id in self._blocked_sent or task.get("blocked"):
            self._blocked_sent.add(task_id)
            return
        _log.info("task %s: blocked — %s", task_id, reason)
        self._client.set_blocked(task_id, True)
        self._blocked_sent.add(task_id)

    def _clear_blocked(self, task: JsonObj) -> None:
        task_id = str(task["id"])
        if task.get("blocked") or task_id in self._blocked_sent:
            self._client.set_blocked(task_id, False)
            _log.info("task %s: blocking condition cleared", task_id)
        self._blocked_sent.discard(task_id)

    @staticmethod
    def _met(task: JsonObj, key: str) -> bool:
        history = task.get("history") or []
        responsibilities = history[-1].get("responsibilities", []) if history else []
        return any(
            r.get("key") == key and r.get("status") == Status.MET.value for r in responsibilities
        )

    def _resolve(
        self, task: JsonObj, key: str, status: Status = Status.MET, comment: str | None = None
    ) -> None:
        if status is Status.MET and self._met(task, key):
            return
        self._client.resolve_responsibility(str(task["id"]), key, status, comment)

    def _advance(self, task: JsonObj, *, note: str | None = None) -> None:
        self._client.apply_operation(str(task["id"]), "advance", note=note)
        _log.info("task %s: %s advanced", task["id"], task["state"])

    def _filing(self, task: JsonObj, repo: JsonObj, slug: str, env: Mapping[str, str]) -> None:
        issue_url = str(task.get("url") or "")
        issue_number = _number(issue_url, _ISSUE_URL)
        if not issue_url:
            found = json.loads(
                self._call(
                    slug,
                    env,
                    "issue",
                    "list",
                    "--search",
                    f"Panopticon task: {task['id']}",
                    "--state",
                    "all",
                    "--json",
                    "number,url",
                )
                or "[]"
            )
            if found:
                issue_number = int(found[0]["number"])
                issue_url = str(found[0]["url"])
            else:
                artifact = None
                if ResidentAgent.ISSUE_ARTIFACT_NAME in self._client.list_artifacts(
                    str(task["id"])
                ):
                    artifact = self._client.get_artifact(
                        str(task["id"]), ResidentAgent.ISSUE_ARTIFACT_NAME
                    ).decode()
                title, body = issue_title_and_body(task, artifact)
                args = ["issue", "create", "--title", title, "--body-file", "-"]
                resident = str(repo.get("resident_agent") or "")
                if resident:
                    args += ["--assignee", resident]
                issue_url = self._call(slug, env, *args, input=body).strip()
                issue_number = _number(issue_url, _ISSUE_URL)
                if not resident:
                    self._block_once(
                        task, "repo has no resident agent configured; issue is unassigned"
                    )
            self._client.set_url(str(task["id"]), issue_url)
        if repo.get("resident_agent"):
            self._clear_blocked(task)
        self._resolve(task, "issue-filed")
        self._advance(task, note=f"GitHub issue #{issue_number}" if issue_number else None)

    def _implementing(self, task: JsonObj, slug: str, env: Mapping[str, str]) -> None:
        if _number(task.get("url"), _PR_URL):
            self._clear_blocked(task)
            self._resolve(task, "pr-opened")
            self._advance(task)
            return
        issue_number = _number(task.get("url"), _ISSUE_URL)
        if issue_number is None:
            self._block_once(task, "task URL is not a GitHub issue")
            return
        timeline = json.loads(
            self._call(
                slug,
                env,
                "api",
                f"repos/{slug}/issues/{issue_number}/timeline",
                "--paginate",
            )
            or "[]"
        )
        for event in timeline:
            source = event.get("source", {}).get("issue", {})
            if event.get("event") == "cross-referenced" and "pull_request" in source:
                pr_url = source.get("html_url") or source["pull_request"].get("html_url")
                if pr_url:
                    self._client.set_url(str(task["id"]), str(pr_url))
                    self._clear_blocked(task)
                    self._resolve(task, "pr-opened")
                    self._advance(task)
                    return
        issue = json.loads(
            self._call(slug, env, "issue", "view", str(issue_number), "--json", "state") or "{}"
        )
        if issue.get("state") == "CLOSED":
            self._block_once(task, "issue closed without a pull request")
            return
        history = task.get("history") or []
        entered = history[-1].get("at") if history else None
        timeout = self._executions.spec(str(task["workflow"])).get("pr_timeout_seconds") or 21600
        if entered and self._wall_clock() - datetime.fromisoformat(
            str(entered).replace("Z", "+00:00")
        ).timestamp() > float(timeout):
            self._block_once(task, "resident has not opened a pull request before the timeout")

    def _review(self, task: JsonObj, slug: str, env: Mapping[str, str]) -> None:
        number = _number(task.get("url"), _PR_URL)
        if number is None:
            self._block_once(task, "task URL is not a GitHub pull request")
            return
        pr = json.loads(
            self._call(
                slug,
                env,
                "pr",
                "view",
                str(number),
                "--json",
                "isDraft,state,mergedAt,closedAt,reviewDecision,reviewRequests,latestReviews",
            )
            or "{}"
        )
        if pr.get("mergedAt"):
            for key in ("pr-ready", "review-requested", "pr-approved"):
                if not self._met(task, key):
                    self._resolve(
                        task,
                        key,
                        Status.FAILED,
                        "merged before approval was observed",
                    )
            self._clear_blocked(task)
            self._advance(task)
            return
        if pr.get("state") == "CLOSED":
            self._block_once(task, "pull request closed without merging")
            return
        self._clear_blocked(task)
        if pr.get("reviewDecision") == "CHANGES_REQUESTED":
            self._client.set_state(
                str(task["id"]), "IMPLEMENTING", note="GitHub review requested changes"
            )
            _log.info("task %s: review requested changes; returning to IMPLEMENTING", task["id"])
            return
        if not pr.get("isDraft"):
            self._resolve(task, "pr-ready")
        if pr.get("reviewRequests") or pr.get("latestReviews"):
            self._resolve(task, "review-requested")
        if pr.get("reviewDecision") == "APPROVED":
            self._resolve(task, "pr-approved")
        ready = not pr.get("isDraft")
        reviewed = bool(pr.get("reviewRequests") or pr.get("latestReviews"))
        if ready and reviewed and pr.get("reviewDecision") == "APPROVED":
            self._advance(task)

    def _merging(self, task: JsonObj, slug: str, env: Mapping[str, str]) -> None:
        number = _number(task.get("url"), _PR_URL)
        if number is None:
            self._block_once(task, "task URL is not a GitHub pull request")
            return
        pr = json.loads(
            self._call(slug, env, "pr", "view", str(number), "--json", "state,mergedAt,closedAt")
            or "{}"
        )
        if pr.get("mergedAt"):
            self._clear_blocked(task)
            self._resolve(task, "pr-merged")
            self._advance(task)
        elif pr.get("state") == "CLOSED":
            self._block_once(task, "pull request closed without merging")
