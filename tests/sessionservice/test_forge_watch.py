"""ForgeWatcher unit tests: all GitHub behavior is replayed by an argv-recording fake."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from panopticon.client import JsonObj
from panopticon.core.models import Status
from panopticon.sessionservice.forge_watch import ForgeWatcher, github_repo_slug


class _Executions:
    def __init__(self, interval: int = 60, timeout: int = 21600) -> None:
        self.interval = interval
        self.timeout = timeout

    def is_forge(self, workflow: str | None) -> bool:
        return workflow == "resident-agent"

    def spec(self, workflow: str) -> JsonObj:
        return {
            "runner_type": "forge",
            "poll_interval_seconds": self.interval,
            "pr_timeout_seconds": self.timeout,
        }


class _Gh:
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], Mapping[str, str] | None, str | None]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> str:
        self.calls.append((list(args), env, input))
        return self.outputs.pop(0) if self.outputs else ""


class _Client:
    def __init__(self, repo: JsonObj, task: JsonObj) -> None:
        self.repo = repo
        self.task = task
        self.urls: list[str] = []
        self.blocked: list[bool] = []
        self.resolved: list[tuple[str, Status, str | None]] = []
        self.advanced: list[str | None] = []
        self.states: list[tuple[str, str | None]] = []
        self.artifact: bytes | None = None

    def get_repo(self, repo_id: str) -> JsonObj:
        return self.repo

    def get_task(self, task_id: str) -> JsonObj:
        return self.task

    def set_url(self, task_id: str, url: str) -> JsonObj:
        self.urls.append(url)
        return self.task

    def set_blocked(self, task_id: str, blocked: bool) -> JsonObj:
        self.blocked.append(blocked)
        self.task["blocked"] = blocked
        return self.task

    def resolve_responsibility(
        self, task_id: str, key: str, status: Status, comment: str | None = None
    ) -> JsonObj:
        self.resolved.append((key, status, comment))
        return self.task

    def apply_operation(self, task_id: str, operation: str, *, note: str | None = None) -> JsonObj:
        assert operation == "advance"
        self.advanced.append(note)
        return self.task

    def set_state(self, task_id: str, state: str, *, note: str | None = None) -> JsonObj:
        self.states.append((state, note))
        return self.task

    def list_artifacts(self, task_id: str) -> list[str]:
        return ["issue.md"] if self.artifact is not None else []

    def get_artifact(self, task_id: str, name: str) -> bytes:
        assert self.artifact is not None
        return self.artifact


def _task(state: str, *, url: str | None = None, blocked: bool = False) -> JsonObj:
    keys = {
        "FILING": ["issue-filed"],
        "IMPLEMENTING": ["pr-opened"],
        "REVIEW": ["pr-ready", "review-requested", "pr-approved"],
        "MERGING": ["pr-merged"],
    }[state]
    return {
        "id": "t1",
        "repo_id": "r1",
        "workflow": "resident-agent",
        "state": state,
        "claimed_by": "host-1",
        "url": url,
        "blocked": blocked,
        "memo": "Fix widgets\nMemo details",
        "initial_prompt": None,
        "history": [
            {
                "at": datetime.now(UTC).isoformat(),
                "responsibilities": [{"key": key, "status": "pending"} for key in keys],
            }
        ],
    }


def _repo(*, resident: str | None = "octo-resident", env_file: str | None = "repo.env") -> JsonObj:
    return {
        "id": "r1",
        "git_url": "https://github.com/acme/widgets.git",
        "env_file": env_file,
        "resident_agent": resident,
    }


def _watcher(
    tmp_path: Path,
    task: JsonObj,
    gh: _Gh,
    *,
    repo: JsonObj | None = None,
    now: list[float] | None = None,
    timeout: int = 21600,
) -> tuple[ForgeWatcher, _Client]:
    (tmp_path / "repo.env").write_text("export GH_TOKEN='secret-token' # comment\n")
    client = _Client(repo or _repo(), task)
    clock = now or [0.0]
    watcher = ForgeWatcher(
        client,  # type: ignore[arg-type]
        _Executions(timeout=timeout),  # type: ignore[arg-type]
        runner_id="host-1",
        run=gh,
        secrets_dir=tmp_path,
        now=lambda: clock[0],
    )
    return watcher, client


def test_filing_creates_assigned_issue_with_artifact_body_and_records_url(tmp_path: Path) -> None:
    task = _task("FILING")
    gh = _Gh("[]", "https://github.com/acme/widgets/issues/12\n")
    watcher, client = _watcher(tmp_path, task, gh)
    client.artifact = b"Artifact details"

    watcher.watch(task)

    create_argv, env, body = gh.calls[1]
    assert create_argv == [
        "gh",
        "issue",
        "create",
        "--repo",
        "acme/widgets",
        "--title",
        "Fix widgets",
        "--body-file",
        "-",
        "--assignee",
        "octo-resident",
    ]
    assert body == "Artifact details\n\nPanopticon task: t1"
    assert env == {"GH_TOKEN": "secret-token", "GH_PROMPT_DISABLED": "1"}
    assert client.urls == ["https://github.com/acme/widgets/issues/12"]
    assert client.resolved[0][:2] == ("issue-filed", Status.MET)
    assert client.advanced == ["GitHub issue #12"]


def test_filing_without_resident_creates_unassigned_and_blocks(tmp_path: Path) -> None:
    task = _task("FILING")
    gh = _Gh("[]", "https://github.com/acme/widgets/issues/12")
    watcher, client = _watcher(tmp_path, task, gh, repo=_repo(resident=None))
    watcher.watch(task)
    assert "--assignee" not in gh.calls[1][0]
    assert client.blocked == [True]


def test_filing_recovers_existing_marker_and_url_short_circuits(tmp_path: Path) -> None:
    task = _task("FILING")
    gh = _Gh(json.dumps([{"number": 12, "url": "https://github.com/acme/widgets/issues/12"}]))
    watcher, client = _watcher(tmp_path, task, gh)
    watcher.watch(task)
    assert len(gh.calls) == 1
    assert client.urls == ["https://github.com/acme/widgets/issues/12"]

    existing = _task("FILING", url="https://github.com/acme/widgets/issues/12")
    gh2 = _Gh()
    watcher2, client2 = _watcher(tmp_path, existing, gh2)
    watcher2.watch(existing)
    assert gh2.calls == []
    assert client2.advanced == ["GitHub issue #12"]


def test_implementing_finds_cross_referenced_pr_and_updates_url(tmp_path: Path) -> None:
    task = _task("IMPLEMENTING", url="https://github.com/acme/widgets/issues/12")
    event = {
        "event": "cross-referenced",
        "source": {
            "issue": {"html_url": "https://github.com/acme/widgets/pull/34", "pull_request": {}}
        },
    }
    watcher, client = _watcher(tmp_path, task, _Gh(json.dumps([event])))
    watcher.watch(task)
    assert client.urls == ["https://github.com/acme/widgets/pull/34"]
    assert client.advanced == [None]


def test_implementing_reentry_with_pr_url_advances_without_gh(tmp_path: Path) -> None:
    task = _task("IMPLEMENTING", url="https://github.com/acme/widgets/pull/34")
    gh = _Gh()
    watcher, client = _watcher(tmp_path, task, gh)
    watcher.watch(task)
    assert gh.calls == []
    assert client.advanced == [None]


def test_implementing_timeout_blocks_once(tmp_path: Path) -> None:
    task = _task("IMPLEMENTING", url="https://github.com/acme/widgets/issues/12")
    task["history"][-1]["at"] = "2020-01-01T00:00:00+00:00"
    gh = _Gh("[]", '{"state":"OPEN"}', "[]", '{"state":"OPEN"}')
    watcher, client = _watcher(tmp_path, task, gh, now=[0.0], timeout=1)
    watcher.watch(task)
    watcher._next_poll["t1"] = 0  # advance a test tick without waiting
    watcher.watch(task)
    assert client.blocked == [True]


def test_implementing_closed_issue_without_pr_blocks(tmp_path: Path) -> None:
    task = _task("IMPLEMENTING", url="https://github.com/acme/widgets/issues/12")
    watcher, client = _watcher(tmp_path, task, _Gh("[]", '{"state":"CLOSED"}'))
    watcher.watch(task)
    assert client.blocked == [True]


@pytest.mark.parametrize(
    ("payload", "resolved", "advanced"),
    [
        (
            {
                "isDraft": True,
                "state": "OPEN",
                "mergedAt": None,
                "reviewDecision": "",
                "reviewRequests": [],
                "latestReviews": [],
            },
            [],
            False,
        ),
        (
            {
                "isDraft": False,
                "state": "OPEN",
                "mergedAt": None,
                "reviewDecision": "APPROVED",
                "reviewRequests": [{"team": {"slug": "owners"}}],
                "latestReviews": [],
            },
            ["pr-ready", "review-requested", "pr-approved"],
            True,
        ),
    ],
)
def test_review_facts_resolve_and_advance(
    tmp_path: Path, payload: dict[str, Any], resolved: list[str], advanced: bool
) -> None:
    task = _task("REVIEW", url="https://github.com/acme/widgets/pull/34")
    watcher, client = _watcher(tmp_path, task, _Gh(json.dumps(payload)))
    watcher.watch(task)
    assert [key for key, _, _ in client.resolved] == resolved
    assert bool(client.advanced) is advanced


def test_review_changes_requested_moves_back_to_implementing(tmp_path: Path) -> None:
    task = _task("REVIEW", url="https://github.com/acme/widgets/pull/34")
    payload = {
        "isDraft": False,
        "state": "OPEN",
        "mergedAt": None,
        "reviewDecision": "CHANGES_REQUESTED",
        "reviewRequests": [],
        "latestReviews": [],
    }
    watcher, client = _watcher(tmp_path, task, _Gh(json.dumps(payload)))
    watcher.watch(task)
    assert client.states == [("IMPLEMENTING", "GitHub review requested changes")]


def test_review_merged_early_fails_unmet_responsibilities_then_advances(tmp_path: Path) -> None:
    task = _task("REVIEW", url="https://github.com/acme/widgets/pull/34")
    watcher, client = _watcher(
        tmp_path, task, _Gh('{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z"}')
    )
    watcher.watch(task)
    assert [status for _, status, _ in client.resolved] == [Status.FAILED] * 3
    assert all(
        comment == "merged before approval was observed" for _, _, comment in client.resolved
    )
    assert client.advanced == [None]


def test_closed_review_blocks_then_open_review_unblocks(tmp_path: Path) -> None:
    task = _task("REVIEW", url="https://github.com/acme/widgets/pull/34")
    closed = {"state": "CLOSED", "mergedAt": None}
    opened = {
        "isDraft": True,
        "state": "OPEN",
        "mergedAt": None,
        "reviewDecision": "",
        "reviewRequests": [],
        "latestReviews": [],
    }
    gh = _Gh(json.dumps(closed), json.dumps(opened))
    watcher, client = _watcher(tmp_path, task, gh)
    watcher.watch(task)
    assert client.blocked == [True]
    watcher._next_poll["t1"] = 0
    watcher.watch(task)
    assert client.blocked == [True, False]


def test_merging_completes_when_merged(tmp_path: Path) -> None:
    task = _task("MERGING", url="https://github.com/acme/widgets/pull/34")
    watcher, client = _watcher(
        tmp_path, task, _Gh('{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z"}')
    )
    watcher.watch(task)
    assert client.resolved[0][:2] == ("pr-merged", Status.MET)
    assert client.advanced == [None]


def test_throttle_and_missing_token(tmp_path: Path) -> None:
    task = _task("FILING", url="https://github.com/acme/widgets/issues/12")
    clock = [0.0]
    gh = _Gh()
    watcher, client = _watcher(tmp_path, task, gh, now=clock)
    watcher.watch(task)
    watcher.watch(task)
    assert len(client.advanced) == 1

    missing = _task("FILING")
    watcher2, client2 = _watcher(tmp_path, missing, _Gh(), repo=_repo(env_file=None))
    watcher2.watch(missing)
    assert client2.blocked == [True]


def test_gh_failure_is_swallowed_for_retry(tmp_path: Path) -> None:
    class _FailingGh(_Gh):
        def __call__(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("gh unavailable")

    task = _task("FILING")
    watcher, client = _watcher(tmp_path, task, _FailingGh())
    watcher.watch(task)  # never raises out of the daemon-facing boundary
    assert client.advanced == []


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/o/n.git", "o/n"),
        ("git@github.com:o/n.git", "o/n"),
        ("ssh://git@github.com/o/n.git", "o/n"),
    ],
)
def test_github_repo_slug(url: str, slug: str) -> None:
    assert github_repo_slug(url) == slug


def test_github_repo_slug_rejects_other_forges() -> None:
    with pytest.raises(ValueError):
        github_repo_slug("https://forgejo.example/o/n.git")


def test_probe_surface_tracks_the_claim(tmp_path: Path) -> None:
    task = _task("FILING")
    watcher, _ = _watcher(tmp_path, task, _Gh())
    assert watcher.is_running("t1") is True
    assert watcher.has_session("t1") is True
    task["claimed_by"] = "other"
    assert watcher.is_running("t1") is False
