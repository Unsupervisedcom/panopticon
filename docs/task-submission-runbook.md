# Submitting work to panopticon — an operator/assistant runbook

How to file, steer, and land tasks on this host's panopticon. Written for an
agent (or human) driving the fleet from outside the containers.

## 1. Filing a task

POST to the task service:

    curl -s -X POST localhost:8000/tasks -H 'Content-Type: application/json' -d '{
      "repo_id": "tarot",
      "workflow": "github-self-reviewed",
      "starting_model": "sonnet",
      "memo": "one-line summary shown on the dashboard",
      "initial_prompt": "the full brief (see §3)",
      "depends_on_task_ids": [],
      "sort_weight": 0
    }'

- **repo_id / workflow**: check `GET /repos` — each repo enables specific
  workflows (`tarot` → `github-self-reviewed`; `panopticon` and
  `unsupervised-main` → `github-peer-reviewed`). Wrong pairing → 400.
- **PR-flow rule**: panopticon-repo PRs target `dimitri/pending-fixes`
  (the repo record's `default_base`), never main. Say so in the prompt.
- **starting_model**: `sonnet` for spec-complete/mechanical work,
  `opus` for design-heavy or cross-file work. Nothing else unless the
  operator asks.
- **depends_on_task_ids**: FULL task ids (truncated ids → 400). Gates
  spawn until the dependency completes.
- **sort_weight**: 0 default; 10 ≈ starred; higher rises (below turn,
  above timestamp). Negative sinks.

**Always verify the filing.** Read the response `id`. If the call errors
client-side, re-query `GET /tasks` and grep the memo before retrying —
POSTs have succeeded while the client reported failure; blind retries
have produced triple-filed duplicates. Check for existing near-duplicate
tasks before filing at all.

## 2. Model of a task's life

PLANNING → (plan review) → ITERATING → REVIEW/MERGING → COMPLETE.
`turn: user` = the operator's ball (plan review, PR review, a question);
`turn: agent` = it's working. Advance/revive/drop with
`PUT /tasks/<id>/state {"state": "ITERATING"|"DROPPED"|…}`.
Set weight later with `PUT /tasks/<id>/sort-weight`.

## 3. Writing the initial_prompt

The prompt is the product. House style, learned the hard way:

- **Verbatim operator quotes** for anything user-decided; label them.
- **Verified pointers**: file:line references you actually checked, exact
  repro commands, measured numbers. Never cite machinery as "merged"
  without checking main *at filing time* — tasks have stalled on claims
  that were true yesterday.
- **Acceptance criteria** with the real-world case named (PR worktrees
  under `~/.cache/tarot/worktrees/...` exist for many unsupervised-main
  PRs; containers can partial-clone with their gh token:
  `gh repo clone Unsupervisedcom/unsupervised-main /tmp/um -- --filter=blob:none --no-checkout`).
- **Verification demands**: tarot work → pty verification per the repo's
  `.claude/skills/verify`, full `uv run pytest -n auto` before the PR,
  subsystem markers for iteration. Both-theme screenshots for anything visual.
- **"Plan first, pause for plan review"** on design-heavy tasks.
- **Scope guards**: what NOT to do; coordination notes naming in-flight
  task ids that touch the same seams ("rebase over X if merged").
- **Standing rule (tarot)**: any PR adding user-visible vocabulary or
  keys updates `docs/guide.md` in the same PR.

## 4. Steering a live task

Each claimed task runs claude in tmux session `panopticon-<taskid>` on
the `-L panopticon` socket, attached to container `panopticon-<taskid>`.

**Never** steer with long `send-keys -l` literals and **never** trust
screen echo — input buffers invisibly on fresh/self-updated panes. Use
the paste-buffer + transcript-receipt protocol:

1. Write the message to a file, prepend a unique marker string.
2. `tmux -L panopticon load-buffer -b inj <file>` then
   `paste-buffer -d -b inj -t panopticon-<taskid>`, then send `Enter`.
3. **Receipt**: `docker exec panopticon-<taskid> grep <marker>
   /home/panopticon/.claude/projects/-workspace/*.jsonl` — poll ~30s;
   retry (the same message; agents dedupe repeats) up to 3×; report
   failure honestly if no receipt.

Gotchas: `panopticon-review-<taskid>` sessions (operator's tarot review
panes) match loose greps — target exact session names. A pane can render
a live-looking UI over a dead claude (check for the process via
`docker exec ... ls /proc/*/comm`-style probes before diagnosing).

## 5. Reading a task's work

- Live pane: `tmux -L panopticon capture-pane -p -S -<n> -t panopticon-<id>`.
- Transcript (live container): `docker exec` and read
  `/home/panopticon/.claude/projects/-workspace/*.jsonl`.
- Transcript (reaped task): the config volume survives —
  `docker run --rm --user root --entrypoint python3 -v panopticon-config-<taskid>:/cfg <base-image> ...`.
- Artifacts: `GET /tasks/<id>/artifacts` / `.../artifacts/<name>` (plan.md etc.).

## 6. Operational gotchas

- **Container images are baked**: changes under `container/` deploy only
  via `make build` + container respawn — a restarted service alone does
  NOT update hooks inside containers.
- **`make stop`/`make start` cycles the whole fleet** (respawn +
  compaction cost); don't restart casually.
- Tasks at `turn: user` sit idle; the fleet moves fast — **re-check task
  and PR state at answer time**, not from memory.
