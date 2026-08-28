# Tasks

## Active

## Backlog

- [ ] You are implementing one feature slice in the panopticon repository (Python; read AGENTS.md first — it is the operating manual: determinism invariant, injectable command runners, no LLMs in tests, long flags, Makefile targets). You are on branch `feat/resident-issue-workflow`, cut fresh from `origin/main`. Work only in this worktree. Do NOT push. Commit in the semantic style used in `git log` (`feat(...)`, `docs(...)`, `test(...)`), in the commit sequence listed at the end.

# Feature: the `resident-agent` workflow + a forge watcher (`runner_type = "forge"`)

## Why
panopticon runs coding tasks in containers. This slice adds a workflow in which panopticon runs NO agent of its own: it **files a GitHub issue, assigns it to a repo's configured resident agent (a long-lived external agent that watches its GitHub notifications), then watches the resident's pull request with the repo's GitHub token** and advances the task deterministically as the PR is opened, leaves draft, gets its code-owner review requested (CODEOWNERS on GitHub decides who reviews — panopticon only observes that a review was requested), gets approved, and is merged. The control plane stays LLM-free: all GitHub access is `gh` shell-outs from the session service behind an injectable command runner, exactly like `sessionservice/kubectl.py` does for kubectl. Nothing in panopticon may name a specific agent or org — the resident is per-repo configuration.

## 1. New runner type `"forge"` — claim, no spawn
- `src/panopticon/core/workflow.py`: extend the `runner_type` ClassVar docstring with `"forge"` — a task the session service *watches* on a forge; nothing is spawned, no container/tmux/clone. No new `__init_subclass__` check needed.
- `src/panopticon/sessionservice/executions.py`: add `is_forge(workflow)` (same shape as `is_shell`/`is_host`/`is_kubernetes`); extend `_FALLBACK_SPEC` and the spec passthrough with `poll_interval_seconds` and `pr_timeout_seconds`.
- `src/panopticon/taskservice/service.py` `workflow_execution` (and the `GET /workflows/{name}/execution` docstring in `taskservice/api.py`): expose `poll_interval_seconds` and `pr_timeout_seconds` (read from the workflow class; `None` for non-forge workflows is fine, or the workflow defaults).
- `src/panopticon/sessionservice/spawner.py`: in `_spawn`, route `is_forge` → new `_spawn_forge(task, repo)` that reports lifecycle `CLAIMING` then `AWAITING` (use the same lifecycle-report helper the other `_spawn_*` use) and returns the synthetic session id `forge-<task_id>`; it creates no task dir, no clone, no tmux. `_runner_for` returns the forge watcher for forge workflows; `_is_orphan` and `startup_reclaim` treat forge like shell (the claim is kept across restarts; nothing is re-run). Constructor gains `forge_watcher: ForgeWatcher | None = None`.
- `src/panopticon/sessionservice/provisioner.py`: skip forge tasks the same way shell tasks are skipped.

## 2. Per-repo resident: `Repo.resident_agent`
- `src/panopticon/core/models.py` `Repo`: add `resident_agent: str | None = None` next to `env_file` — the forge login an issue is assigned to. Docstring: per-repo because a resident belongs to a forge org, not to a workflow.
- Alembic migration in `migrations/versions/` (follow the existing file naming/pattern, e.g. the task-url migration): nullable text column `resident_agent` on `repo`. Keep `tests/test_migrations.py`'s create_all-vs-migrations drift check passing.
- SQLAlchemy store adapter column; `taskservice/api.py` `RepoOut` + repo create/update inputs; `client.py` if repos are typed there; the dashboard repo form (`terminal/dashboard.py`) gets a `resident agent` field next to `env_file` (keep the change minimal and consistent with the existing form fields).

## 3. Workflow `src/panopticon/workflows/resident_agent.py`
```python
class ResidentAgent(Workflow):
    name = "resident-agent"
    opt_in = True
    runner_type = "forge"
    when_to_use = ("Delegate a change to the repo's resident agent: panopticon files a GitHub issue, "
                   "assigns it to the resident, and tracks the resident's PR through code-owner review to merge. "
                   "No container, no local agent.")
    poll_interval_seconds: ClassVar[int] = 60
    pr_timeout_seconds: ClassVar[int] = 6 * 3600
    ISSUE_ARTIFACT_NAME: ClassVar[str] = "issue.md"
```
Subclass `Workflow` directly (no skills, tools, image layer; `shell_script()` stays default). States (nested classes as in `workflows/github_peer_reviewed.py`), ALL with `advanced_by = Actor.AGENT` (the deterministic watcher is the actor; the user's moves are `drop` and the free `set_state`):

| State | label | responsibilities (key: description) | transitions |
|---|---|---|---|
| Filing (InitialState) | FILING | `issue-filed`: the issue exists on the forge, is assigned to the repo's resident agent, and its URL is recorded on the task | ("IMPLEMENTING",) |
| Implementing | IMPLEMENTING | `pr-opened`: a pull request referencing the issue exists and its URL is recorded on the task | ("REVIEW",) |
| Review | REVIEW | `pr-ready`: the PR is not a draft; `review-requested`: a review has been requested from (or submitted by) a code owner; `pr-approved`: the PR's review decision is APPROVED | ("MERGING",) |
| Merging | MERGING | `pr-merged`: the PR is merged | (Complete,) |

`initial = Filing`. Give each state a one-line `description`. Override `_overview_extras`/`_briefing_extras` (see `planned_workflow.py` for the pattern) with a short explanation that no agent runs for this task, that the resident implements and code owners review on GitHub, and that the task `url` is the issue until a PR exists, then the PR. Register in `workflows/__init__.py`.

Issue title/body sourcing (a pure helper in the workflow module, unit-tested): title = first line of the task `memo`; body = the `issue.md` artifact if present, else `initial_prompt`, else the memo's remaining lines; always append a final line `Panopticon task: <task_id>` (plain text, so `gh issue list --search` can find it — HTML comments are not indexed).

## 4. Watcher `src/panopticon/sessionservice/forge_watch.py`
Model it on `sessionservice/kubectl.py` + `kubernetes_runner.py`:
- `GhRunner` Protocol: `__call__(args: Sequence[str], *, check: bool = True, env: Mapping[str, str] | None = None) -> str`; production `subprocess_run` uses `subprocess.run(..., capture_output=True, text=True, env={**os.environ, **env})`. `gh_argv(gh: str, repo_slug: str, *args)` always passes `--repo <slug>`. Long flags only.
- `github_repo_slug(git_url) -> str` returning `owner/name` for `https://github.com/o/n(.git)`, `git@github.com:o/n(.git)`, `ssh://git@github.com/o/n.git`; raise `ValueError` otherwise.
- Token: resolve the repo's env file with `secrets_file_path(repo["env_file"], secrets_dir=...)` (`core/dirs.py`), parse `KEY=VALUE` lines (tolerate `export `, quotes, comments) and pass `{"GH_TOKEN": token, "GH_PROMPT_DISABLED": "1"}` as env to every `gh` call. Missing env_file or `GH_TOKEN` → `client.set_blocked(task_id, True)` once (remember per task so it is not re-sent every tick) with a log line; make no `gh` call.
- `class ForgeWatcher(client, executions, *, runner_id, run=subprocess_run, gh="gh", secrets_dir=None, now=time.monotonic)`. Runner-probe surface so `Spawner._runner_for`/reconcile/heal need no new branches: `is_running(task_id)` and `has_session(task_id)` return True while the task is claimed by this runner; `stop(task_id)` is a no-op; `attach_command` (if the Runner ABC requires it) returns None/raises NotImplementedError with a clear message. Implement the `Runner` ABC (`sessionservice/runner.py`) if that is what `_runner_for` expects.
- `watch(task: JsonObj) -> None`: gates — `task["claimed_by"] == runner_id`, `executions.is_forge(task["workflow"])`, state not terminal (`core.state.TERMINAL_LABELS`), and a per-task throttle (`now() >= next_poll[task_id]`, interval = the workflow's `poll_interval_seconds` from `executions`). Then ONE idempotent step for the current state; every step re-reads facts from `gh`, keeps all durable state in the task record (so a daemon restart resumes where it left off), skips re-resolving a responsibility already MET, and clears `blocked` when the blocking condition has cleared. Use the task-service client methods that exist (`set_url`, `resolve_responsibility`, `apply_operation("advance")`, `set_state`, `set_blocked`, `get_repo`) — check `client.py` and add thin methods only if missing.
  - **FILING**: if `task["url"]` is set → resolve `issue-filed` MET, `advance`. Else `gh issue list --repo <slug> --search "Panopticon task: <task_id>" --state all --json number,url` (crash-between-create-and-set_url guard); if none, build title/body via the workflow helper (read `issue.md` through the client's artifact read if present) and `gh issue create --repo <slug> --title <t> --body-file - --assignee <repo.resident_agent>` (body via stdin — extend the runner protocol with `input: str | None` if needed). If `resident_agent` is empty: create the issue unassigned and `set_blocked(True)` with a note that the repo has no resident agent configured. Then `set_url(issue_url)`, resolve, advance. Record the issue number in the transition `note`.
  - **IMPLEMENTING**: issue number = parse from the issue url (the url is the issue while no PR is known; once a PR url is recorded, detect it by path `/pull/`). `gh api repos/<slug>/issues/<n>/timeline --paginate` → first `cross-referenced` event whose `source.issue.pull_request` exists → `set_url(pr_html_url)`, resolve `pr-opened`, advance. If the PR url is already recorded (re-entry after CHANGES_REQUESTED) resolve + advance immediately. No PR and `now - <entered_at>` > `pr_timeout_seconds` (use the current history entry's `at`; parse ISO) → `set_blocked(True)` once. Issue closed with no PR → blocked.
  - **REVIEW**: `gh pr view <n> --repo <slug> --json isDraft,state,mergedAt,closedAt,reviewDecision,reviewRequests,latestReviews`. Resolve `pr-ready` when `isDraft` is false; `review-requested` when `reviewRequests` is non-empty (user or team entries) OR `latestReviews` is non-empty; `pr-approved` when `reviewDecision == "APPROVED"`. All three MET → `advance`. `reviewDecision == "CHANGES_REQUESTED"` → `set_state("IMPLEMENTING")` with a note (the free move; the PR url stays). `state == "CLOSED"` and no `mergedAt` → blocked. `mergedAt` set while still in REVIEW → resolve any unmet responsibilities as FAILED with a comment ("merged before approval was observed") and advance.
  - **MERGING**: `gh pr view ... --json state,mergedAt,closedAt`; `mergedAt` → resolve `pr-merged`, advance (→ COMPLETE). Closed unmerged → blocked.
- Log one line per state change at INFO; never raise out of `watch` for a `gh` failure — log and retry next poll.

## 5. Host daemon wiring
- `src/panopticon/sessionservice/host.py`: `HostDaemon.__init__` gains `watcher: ForgeWatcher | None`; `tick` calls `self._watcher.watch(task)` for each task after `provision`, isolating exceptions per task like the other steps. `main`/`build_*` constructs ONE shared `WorkflowExecutions` passed to Spawner, Provisioner and ForgeWatcher (check how Spawner builds its own today and pass the same instance). Add `--gh` (binary override, default `gh`) mirroring `--kubectl`. No other CLI flag.

## 6. Tests (pytest, no LLMs, fakes for `gh`)
- `tests/workflows/test_resident_agent.py` modeled on `tests/workflows/test_github_peer_reviewed.py`: initial state/turn, transitions per state, every state `advanced_by == Actor.AGENT`, responsibilities per state, gating (`ResponsibilitiesNotMet`), free move REVIEW→IMPLEMENTING via `force_transition`, drop from every state, `runner_type == "forge"`, `skills()` empty, issue title/body helper cases (memo only; artifact wins; footer always present), briefing mentions the url convention. Bump the expected built-in count in `tests/workflows/test_discovery.py`.
- `tests/sessionservice/test_forge_watch.py`: a `_Gh` fake keyed by argv prefix replaying canned JSON and recording calls + env (template: `tests/sessionservice/test_kubernetes_runner.py` fake runner). Cases: FILING creates with title/body footer and `--assignee <resident>` and records the url; no resident → unassigned + blocked; existing marker issue → no create; FILING with url already set → no gh call, advance; IMPLEMENTING finds the PR via timeline and flips url; no PR past timeout → blocked once; REVIEW draft → only `pr-ready` unresolved; team review request satisfies `review-requested`; APPROVED → advance; CHANGES_REQUESTED → `set_state("IMPLEMENTING")`; closed unmerged → blocked, then a later PR unblocks; MERGING mergedAt → COMPLETE; throttle honors `poll_interval_seconds`; missing GH_TOKEN → blocked and zero gh calls; env passed to gh contains `GH_TOKEN` and `GH_PROMPT_DISABLED`; `github_repo_slug` for all URL forms + ValueError; `is_running`/`has_session` semantics. Use a fake task-service client (check how `test_spawner.py`/`test_host.py` fake it and reuse).
- `tests/sessionservice/test_executions.py`: `is_forge`, fallback fields. `test_spawner.py`: forge task is claimed, reports CLAIMING/AWAITING, no clone/docker/tmux calls, not an orphan, `startup_reclaim` keeps the claim. `test_host.py`: `tick` calls the watcher per task and isolates its failure. `test_provisioner.py`: forge skipped. Repo API tests: `resident_agent` round-trips through create/update/get. `tests/test_migrations.py` stays green.

## 7. Docs
- `docs/design/decisions/0015-forge-watched-resident-workflows.md` (same shape as ADR 0014): Context (delegation to residents; ADR 0010 chose pull over webhooks; the determinism invariant), Decision (`runner_type="forge"` = claim without spawn; the watcher lives in the session service behind an injectable `gh` runner; the resident is per-repo configuration, the reviewer is whatever CODEOWNERS says; merge is tracked not gated — the human gate is code-owner approval on the forge), Alternatives considered (a `runner_type="shell"` bash poller in a tmux pane — untested logic, a pane per task for days, `startup_reclaim` never re-runs shell scripts so a restart strands it; webhooks; an LLM agent as tracker), Consequences (poll cadence, dashboard shows `awaiting` while watched, `advanced_by=AGENT` now also means "a deterministic actor").
- `AGENTS.md` module map: a line for `sessionservice/forge_watch.py` and mention `ResidentAgent` in the `workflows/` entry; add the new test file to the "tests worth knowing" section if one exists.
- `docs/workflows/resident-agent.md` + a row in `docs/workflows/README.md` if that table exists (check; otherwise skip).
- `docs/design/BACKLOG.md`: add items — a `WATCHING` lifecycle phase/container status for forge tasks; fold the per-task poll throttle into the shared poll abstraction (existing P3 item); GraphQL `closingIssuesReferences` as a stricter issue↔PR link; optional `merge_gate` ClassVar (user-gated MERGING); a Forgejo forge backend.

## Verification (run it, report the output)
`direnv`/devenv provides the toolchain: run `devenv shell -- make check` (ruff check + format check, mypy strict, pytest). If devenv is unavailable in your sandbox, run `uv run ruff check . && uv run ruff format --check . && uv run mypy --package panopticon && uv run pytest -q`. Fix everything until green. Finish with `git status` and `git log --oneline origin/main..HEAD` in your final message.

## Commits (in this order)
1. `feat(sessionservice): add the forge runner type — claim a task without spawning` (executions, spawner, provisioner, workflow docstring, service/api execution fields + their tests)
2. `feat(repo): per-repo resident_agent` (model, migration, store, api, client, dashboard form + tests)
3. `feat(workflows): resident-agent workflow` (workflow module, registry, tests, discovery count)
4. `feat(sessionservice): forge watcher drives resident-agent tasks over gh` (forge_watch.py, host wiring, tests)
5. `docs: ADR 0015 forge-watched resident workflows` (ADR, AGENTS.md, docs/workflows, BACKLOG)
