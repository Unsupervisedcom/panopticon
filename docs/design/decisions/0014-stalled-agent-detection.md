# 0014 — Stalled-agent detection & recovery

- Status: Accepted
- Date: 2026-07-30
- Deciders: Charlie Scherer

## Context

Task f088043b originally scoped this as "detect a dead `claude` process and respawn it." The
operator dropped it and reopened with a broader, corrected report: the common failure isn't the
process dying — it's a transient API failure (overloaded/5xx/timeout/usage-limit) that aborts an
in-flight request and leaves claude's CLI idling at its own prompt, alive but stuck. Turn tracking
(`container/hooks.py`) is purely event-driven (`Stop`/`UserPromptSubmit`); nothing fires when the
agent hangs mid-turn, so `Task.turn` reads `"agent"` indefinitely and the task looks like a
healthy, working agent everywhere the dashboard/API look. Fleet data corroborated it: tasks
`ITERATING` at `turn=agent` for 4–10h with token burn far too low for real work. Today an operator
notices the silence and manually types "try again" into the pane.

A concrete incident grounded the design: a fleet-wide stall 23:30–03:38 UTC (07-22/23), four
concurrent tasks stalled ~4h each. Task `8fa7de51`: last transcript record a `tool_result` at
23:37, next `assistant` record not until 03:38 — the tool had already finished, so the stall was
in the *next* LLM call, not a still-running tool. Two of the four were revived only by a manual
operator message, i.e. whatever retry claude's own CLI attempts internally didn't resolve at least
half of them — external intervention is genuinely necessary, not just patience.

A plan-review addendum added a second, load-bearing fact, verified empirically across 60 sampled
task volumes: **claude never writes API-error banners to its transcript.** Zero error records
despite known multi-hour stalls. This ruled out the original design (three symmetric signals —
transcript content, process table, pane content — voted together) and drove the actual shape
below.

Two stall shapes needed a single design: (A) claude's process alive but idle-errored (the common
case per the operator), and (B) claude's process gone from an otherwise-live container (usage-limit
exhaustion, a hard crash, OOM). `spawner.py::heal()` already self-heals a claimed task whose tmux
session is gone — but it's gated on session existence, not on `claude` specifically, so it already
covers a good chunk of "clean death" (nothing in the repo sets tmux's `remain-on-exit`, so a pane's
foreground process exiting for any reason takes the session down with it by default, and `heal()`
picks that up). What's uncovered — the gap this ADR closes — is a **wedged** process: the container
and tmux session both stay up, `is_running`/`has_session` both read healthy, and nothing today
distinguishes that from real, silent, legitimate work (a long `pytest` run, a long streamed
generation).

## Decision

### 1. Detection is staleness-driven, not a symmetric multi-signal vote

Since the transcript carries no error text, it has exactly one useful property: whether it's
*still growing*. `LocalRunner.transcript_mtime` (`docker exec find … -printf '%T@\n'` — the
per-task claude config dir is a **named Docker volume**, not a host bind mount, so this is the
only way to read it) gives an age; once that age crosses `PANOPTICON_STALL_IDLE_MINUTES` (default
8 — conservative, and would have cut the reference incident's ~4h stall to a small fraction of
that) while `turn == "agent"`, the task becomes a stall *candidate* — not yet a stall.

### 2. Two suppression guards, not peer signals

A transcript gap is not evidence of a stall by itself — it's also what a still-running tool call
or a long single generation look like from the host. Before a candidate is acted on:

- **`LocalRunner.process_snapshot`** (`docker exec ps -eo pid,ppid,comm --no-headers`, parsed by
  the pure `parse_process_snapshot`): if `claude` has a live descendant (a tool subprocess), the
  gap is explained — reset the candidate. This is shaped directly against the reference incident:
  its stall began the moment a tool result *landed*, so a naive "was a tool recently active" check
  must not treat the already-finished call as ongoing.
- **`LocalRunner.pane_text`** (`tmux capture-pane`): if the pane's content changed since the last
  probe, real work is visible (a still-streaming generation) even though the transcript hasn't
  appended yet — reset the candidate.

Only once transcript age crosses the threshold **and** neither guard has fired across a full probe
interval does `StallMonitor` classify and act. Probing itself is throttled independently of the
host loop's ~2s tick (`PANOPTICON_STALL_PROBE_INTERVAL_SECONDS`, default 60s) so a fleet of
healthy, live tasks doesn't cost a `docker exec`/`tmux capture-pane` pair every tick.

### 3. The tmux pane is the only source of classification text

Since the transcript carries none, `classify_pane_text` (pure, `sessionservice/stall.py`) reads
the captured pane against known claude CLI failure signatures (`overloaded_error`, 5xx,
rate-limit, network/timeout, and a usage-limit pattern that also extracts the printed reset time).
It's deliberately lenient: unrecognized text still yields a cause (`unknown_error`), never `None`
— the operator's report is explicit that the errors are heterogeneous, and the reference
incident's two manually-revived tasks are exactly the case that might not match a known signature.
Recovery must not require an exhaustive signature list.

### 4. Recovery: shape-appropriate, not uniform

`process_snapshot` also answers the shape A/B question at the point of action:

- **Shape A (claude present)**: `LocalRunner.send_keys` types a retry prompt (default `"try
  again"`) into the live pane — the direct automation of what the operator does manually today.
  Chosen over a full container respawn because the process is alive and undamaged: a respawn tears
  down and recreates the container, re-execs claude, and reloads the transcript from disk, all to
  accomplish what one keystroke does with far less disruption and latency. This is the one
  genuinely debatable call in the design (the alternative — `--continue` + `INTERRUPT_PROMPT` via
  a full respawn, which `agent.py` already supports — was explicitly on the table); both paths
  share the same detection/classification/budget machinery, so switching later is a one-line
  change in `StallMonitor`, not a redesign.
- **Shape B (claude absent)**: `Spawner.respawn(task)` — a thin public wrapper added around the
  same `_spawn()` path `heal()` already uses (kill any stale session/container, spawn fresh;
  `agent.py` resumes via `--continue` + `INTERRUPT_PROMPT` since `turn` is still `"agent"`).
  Reused verbatim rather than duplicated.
- **Usage-limit specifically**: when a reset time is confidently parsed from the pane, the monitor
  doesn't retry immediately — it schedules the next action for that time (surfaced in the
  lifecycle detail) regardless of which shape it turns out to be once that time arrives. Other
  causes get bounded exponential backoff (2/4/8/16 minutes). A cap
  (`PANOPTICON_STALL_MAX_RECOVERIES`, default 3) stops the loop and surfaces loudly instead of
  thrashing.

### 5. Surfacing: a new `STALLED` phase overrides `LIVE`

`compose_container_status` (`core/models.py`) previously short-circuited to `LIVE` the instant a
container registration was open, **regardless of reported phase** — a deliberate, tested behavior
("the container holds its own `/live` connection independent of anything else"). A stalled task
is, by definition, registered and alive at the transport level, so this needed a precedent-
breaking exception: `LifecyclePhase.STALLED`/`ContainerStatus.STALLED` are checked *before* the
`registered → LIVE` shortcut (a one-line `frozenset` of phases that override it, `{STALLED}`
today), so a stalled-but-registered container reads `stalled`, not `live`. Every other phase's
existing "registered wins" behavior is untouched. The free-text `lifecycle_detail` (already
exposed unconditionally on `TaskOut`) carries the evolving message: `"api-error: auto-retry 2/3
(cause=overloaded_error)"`, `"usage limit: retrying at 21:00"`, or, once exhausted, `"stalled:
3/3 auto-recoveries exhausted (cause=…) — needs manual bump"`.

### 6. Manual override needs no new plumbing

`TaskService.claim()`/`release()` already clear any reported lifecycle phase, and the dashboard's
`R` (force-respawn) already goes through `release()`. `StallMonitor`'s own per-task bookkeeping
(recovery count, backoff, pane baseline) is dropped the moment a probe observes the task healthy
again — a **stricter** reset than `Spawner`'s respawn survivor-window (which only resets after
surviving a fixed window): for a stall, "the transcript resumed growing" is direct proof of
health, not just "the container came up," so there's nothing left to decay. This covers both an
automatic recovery succeeding and a manual bump transparently — both are observed the same way,
through the same transcript-growth signal.

### 7. `procps` added to the base image

`LocalRunner.process_snapshot`'s `docker exec … ps …` assumed `ps` exists in the task container;
the base Dockerfile didn't install it (`python:3.13-slim` doesn't include `procps`). Added
alongside the other CLI dependencies the control plane's own tooling needs (`git`, `gosu`, …) —
discovered and fixed while building this feature's acceptance tests against a real container.

## Consequences

**Positive**

- Closes the actual gap: a wedged-but-alive agent, previously indistinguishable from a healthy
  one everywhere, is now detected, classified, nudged, and — if that fails repeatedly — surfaced
  loudly instead of silently burning hours.
- Shares machinery with existing self-heal (`Spawner.respawn`/`heal()`'s crash-loop-budget shape)
  rather than inventing a parallel recovery path.
- No LLM calls anywhere in the detection/recovery path — it inspects processes/logs and drives
  `docker`/`tmux`, staying inside the `sessionservice` determinism boundary.

**Negative / deferred**

- `classify_pane_text`'s regexes (especially the usage-limit reset-time parser,
  `parse_reset_time`) are seeded from known claude CLI message shapes, not real captured text from
  the reference incident — its pane content wasn't retrievable after the fact (tmux doesn't
  persist scrollback once a session's gone, and nothing pipes panes to a log today). The lenient
  `unknown_error` fallback means detection/recovery isn't gated on getting every signature right,
  but the usage-limit scheduling specifically only fires when the reset-time regex matches.
- `docker exec`-based probing has a real cost at fleet scale; the probe-interval throttle bounds
  it per task, but a host with many simultaneously-live tasks issues one `ps`/`find`/tmux
  round-trip per candidate per interval. Not measured against a large fleet.
- No dedicated stall-event persistence beyond structured log lines + the lifecycle-detail history.
  This has a concrete downstream cost, not just a hypothetical one: the task time-profiler
  (`profiler/`, merged into this base branch as this task was in flight) bills every
  user→assistant transcript gap to `llm` time — exactly the shape a stall takes (the reference
  incident's own gap was `tool_result` → next `assistant`, hours later), so a stalled task's
  profile currently reads as an implausibly long `llm` span rather than being flagged. Reconciling
  the two is a follow-up (see BACKLOG.md), not pre-built here.

## Related

- ADR 0008 — execution/session topology; `LocalRunner`/`Spawner` conventions this extends.
- `profiler/` (merged into `dimitri/pending-fixes` as `#349`, landed after this task's plan was
  written) — the retroactive gap-analysis this ADR's "Negative" section flags as currently
  mis-attributing stall gaps; see the BACKLOG.md entry for candidate fixes.
- ADR 0011 — the per-task config volume (`CONFIG_MOUNT`) `transcript_mtime` reads via `docker
  exec`, since it isn't a host bind mount.
- `docs/design/BACKLOG.md` — `tmux pipe-pane` forensic logging (so a *future* incident's exact
  pane text is recoverable) and richer stall-event persistence, both deferred here.
