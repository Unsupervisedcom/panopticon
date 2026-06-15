# Parity checklist — cloude-cade → panopticon

Feature inventory mined from [cloude-cade](https://github.com/tildesrc/cloude-cade)
(README, CLAUDE.md, docs/internals.md, `.claude/commands/`, `bin/`, Makefile, tests).
This is the **spec-by-example**: every user-facing capability of the prototype, so the
rewrite doesn't silently lose behavior.

**How to use:** fill the **K/C/D** column for each row —
- **Keep** — reproduce the behavior as-is.
- **Change** — keep the capability but rework it (e.g. behind an interface, generalized for
  flexible workflows).
- **Drop** — intentionally not carried over.

The **Hint** column flags where an ADR or milestone already implies a direction — these
are suggestions, not decisions.

> **Parity scope:** Milestone 1 = parity with this workflow **plus** a free-form
> workflow. Anything tied to the *specific* cloude-cade lifecycle is a "Change" candidate
> because the lifecycle becomes one configurable workflow among several (Milestone 1).

---

## 1. Task lifecycle / state machine

| Capability                                                  | What it does                                                          | K/C/D | Hint                                            |
| ----------------------------------------------------------- | --------------------------------------------------------------------- | ----- | ----------------------------------------------- |
| States `PLANNING → ITERATING → REVIEW → MERGING → COMPLETE` | The core hardcoded lifecycle                                          | C     | Change — becomes a *configurable* workflow (M1) |
| Terminal `DROPPED` state                                    | Abandoned-task terminal state                                         | K     |                                                 |
| Foreground vs. background                                   | PLANNING/ITERATING/REVIEW need user approval; MERGING is agent-driven | K     | Keep concept; per-workflow (M1)                 |
| User-driven forward transitions                             | Only `MERGING → COMPLETE` auto-advances                               | K     | Per-workflow policy (M1)                        |
| `:SKIP_REVIEW:` bypass                                      | `ITERATING → MERGING`, skipping REVIEW                                | C     | Likely subsumed by configurable workflow (M1)   |
| Initial state by mode                                       | Standard starts `PLANNING`; ADOPT starts `ITERATING`                  | K     |                                                 |
| Per-stage audit trail                                       | Appends timestamped log entry on each transition                      | C     | Maps to history log (ADR 0001)                  |

## 2. Host-side commands

| Capability             | What it does                                                                                 | K/C/D | Hint                                    |
| ---------------------- | -------------------------------------------------------------------------------------------- | ----- | --------------------------------------- |
| `/promote`             | Activate a staging idea (or adopt a PR): branch, draft PR, worktree, task file, tmux session | C     |                                         |
| `/sweep`               | Surface COMPLETE/DROPPED tasks ready for cleanup; confirm each                               | K     |                                         |
| `/finalize`            | 8-step teardown of a terminal task (tmux, worktree, volume, branch, archive file)            | C     |                                         |
| `/suggest-slugs`       | Generate slugs for slugless staging ideas                                                    | C     | Tied to org staging (ADR 0001 → change) |
| `/suggest-slugs-watch` | Background watcher auto-triggers slug suggestion                                             | D     | Tied to org staging (ADR 0001 → change) |

## 3. In-container agent commands

| Capability       | What it does                                                               | K/C/D | Hint                 |
| ---------------- | -------------------------------------------------------------------------- | ----- | -------------------- |
| `/advance`       | Progress to next stage; evaluate Definition-of-Done; confirm               | K     | Workflow-driven (M1) |
| `/iterate`       | Return to coding from REVIEW/MERGING                                       | C     | Workflow-driven (M1) |
| `/drop`          | Abandon a non-terminal task                                                | K     |                      |
| `/babysit-ci`    | Autonomous CI watch/fix loop (retry≤3, 2h budget, conflict handling)       | C     |                      |
| `/babysit-merge` | Autonomous merge-queue shepherding (auto-squash, requeue, revert on block) | C     |                      |

## 4. Dashboard (`cloude-dash`)

| Capability                  | What it does                                                                                                            | K/C/D | Hint                                    |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----- | --------------------------------------- |
| Task list by stage priority | Active, then staging, then recently finalized                                                                           | K     | Behind presentation interface (ADR 0002)     |
| State + tag badges          | `[ITERATING :user:]` etc.                                                                                               | K     |                                         |
| Multi-repo labels           | Repo column for cross-repo visibility                                                                                   | K     |                                         |
| Keybindings                 | nav (↑↓/hjkl), `p` open PR, `t` tmux switch, `c` copy slug, `P` promote, `f` finalize, `/` filter, `r` reload, `q` quit | K     |                                         |
| Live file monitoring        | inotify (Linux) / mtime fallback; auto-reload; identity across reorders                                                 | C     | Source becomes DB, not files (ADR 0001) |

## 5. Task storage & file format

| Capability                                   | What it does                                                             | K/C/D                           | Hint                                 |
| -------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------- | ------------------------------------ |
| org-mode task files                          | One `.org` per task as source of truth                                   | D                               | **Change** — DB for state (ADR 0001) |
| `staging.org` ideas file                     | Lightweight idea capture w/ `:REPO:`, `:ADOPT:`, `:SLUG:`, `:COMPANION:` | C - dashboard allows idea input | Change (ADR 0001)                    |
| Directory layout `staging / active/ / done/` | Lifecycle-by-directory                                                   | C                               | Change (ADR 0001)                    |
| `** Plan` section                            | Plan narrative, seeded on promote / plan-accept                          | C                               | **Keep as file artifact** (ADR 0003) |
| `** Work` section                            | Implementation notes                                                     | C                               | Artifact or DB note (ADR 0003)       |
| `** Log` section                             | Structured per-stage audit entries                                       | C                               | DB history (ADR 0001)                |
| Dual org TODO sequences                      | Stage keywords + verdict keywords                                        | C                               | org-specific → change                |
| Slug format / derivation                     | `YYYY-MM-DD-<slug>`, `[a-z0-9-]`, 3–5 words                              | K                               | Keep id convention                   |

## 6. Definition-of-Done (DoD) & verdicts

| Capability                     | What it does                                        | K/C/D | Hint                  |
| ------------------------------ | --------------------------------------------------- | ----- | --------------------- |
| Per-stage DoD checklist        | Canonical bullets per stage, evaluated on advance   | K     | Workflow-defined (M1) |
| Verdict states                 | `PENDING` / `PASS` / `UNSATISFIABLE` gating turns   | K     |                       |
| Verdict consistency validation | Stop hook blocks inconsistent PASS/PENDING          | K     |                       |
| Auto-tick "user approved plan" | On PLANNING exit via `/advance` or plan-mode accept | K     |                       |

## 7. Container / sandbox model

| Capability                | What it does                                                          | K/C/D                                                                                                                                 | Hint                               |
| ------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Per-task Docker container | Isolated container per task, UID/GID-matched                          | K                                                                                                                                     | Behind execution-backend interface (M5) |
| Docker-in-Docker          | Per-task `dind` engine + volume                                       | C - configurable per repo                                                                                                             |                                    |
| Per-task tmux session     | `cloude-<slug>`, 2-window (agent + read-only task view)               | K                                                                                                                                     |                                    |
| Base image                | Node20/Bookworm + git/tmux/jq/gh/claude/uv/bun                        | C - start w/ minimal image to support claude and other general panopticon requirements, allow worksflows and repos to layer onto this |                                    |
| Volume mounts             | RO root, RW task dir + worktree, per-repo creds, per-task dind        | C - structure likely different                                                                                                        |                                    |
| Entrypoint sequence       | Creds persist, command registration, dind init, privilege drop (gosu) | C - per configurable images                                                                                                           |                                    |
| Prefill prompt            | Pre-populate Claude input on launch (bracketed-paste detect)          | K                                                                                                                                     | claude-specific → Slice 6          |
| Read-only task-file pane  | Auto-reverting editor view of the task                                | D                                                                                                                                     |                                    |

## 8. Integrations (git / GitHub)

| Capability                             | What it does                                     | K/C/D                                   | Hint |
| -------------------------------------- | ------------------------------------------------ | --------------------------------------- | ---- |
| `gh` PR create / view / checks / merge | PR lifecycle + CI + merge queue                  | C - workflow specific                   |      |
| Feature branch `cloude/<slug>`         | Branch on default base                           | K - core/agnostic (rename only; not workflow-specific) | local git, not forge |
| Per-task worktree                      | `worktrees/<repo>/<branch>`                      | K                                       |      |
| Worktree teardown tiers                | standard / `--force-worktree` / `--force-root`   | K                                       |      |
| ADOPT-mode PR checkout                 | Track existing PR branch, no push                | C - workflow specific                   |      |
| Repo-specific pre-launch hooks         | `repo-hooks/<repo>` filters config before launch | C - built into repo configurable images |      |

## 9. Configuration & secrets

| Capability                  | What it does                                                                                     | K/C/D                                                                 | Hint                                          |
| --------------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- | --------------------------------------------- |
| Per-repo credentials volume | `cloude-claude-creds-<repo>` persists OAuth                                                      | K + generalize for other secrets                                      | **Keep + generalize** — secrets per repo (M1) |
| `:REPO:` per project        | Repo URL on staging project; groups ideas                                                        | C                                                                     | Repo entity (ADR 0001 / M1)                   |
| Env vars                    | `CLOUDE_TASK_FILE`, `CLOUDE_ROOT`, `REPO`, `GH_TOKEN`, `TZ`, dry-run/no-watch/no-prefill toggles | C - GH_TOKEN becomes part of the secret store, others probably rename |                                               |
| Multi-repo support          | Isolated creds, worktrees, staging grouping per repo                                             | K                                                                     | Keep (M1 builds on this)                      |
| `make login REPO=`          | Interactive per-repo auth                                                                        | K                                                                     | Generalize for other CLIs (M3)                |

## 10. Hooks & automatic tag management

| Capability                          | What it does                                          | K/C/D                                   | Hint                                |
| ----------------------------------- | ----------------------------------------------------- | --------------------------------------- | ----------------------------------- |
| `:agent:` ↔ `:user:` tag flips      | Track who holds the ball across turns/questions       | K - probably the most important feature |                                     |
| Plan-accepted hook                  | Auto-advance PLANNING→ITERATING, write plan, tick DoD | C - workflow specific                   |                                     |
| User-prompt / stop / question hooks | Tag flips + DoD consistency enforcement               | K                                       | claude-hook-specific → Slice 6      |
| `:blocked:` tag preservation        | Deliberate agent marker survives auto-flips           | K                                       |                                     |
| Host session-start hook             | Arms slug watcher                                     | D                                       |                                     |

## 11. Special modes

| Capability       | What it does                                    | K/C/D                                   | Hint                        |
| ---------------- | ----------------------------------------------- | --------------------------------------- | --------------------------- |
| ADOPT mode       | Promote an existing open PR; start in ITERATING | C - implement as workflow               |                             |
| COMPANION mode   | Cross-repo paired tasks via `:COMPANION:`       | D - initially, add to backlog for later |                             |
| Skip-review mode | `:SKIP_REVIEW: t` jumps ITERATING→MERGING       | C - support w/ separate workflow        | Subsumed by workflows? (M1) |

## 12. Cleanup & resource management

| Capability              | What it does                                                                                   | K/C/D                                                              | Hint |
| ----------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---- |
| 8-step finalize cleanup | metadata → PR validate → tmux kill → worktree rm → volume rm → branch del → archive → summary  | C - keep workflow agnostic parts, refactor workflow specific parts |      |
| Confirmation exit codes | 10–15 gate risky cleanup (unmerged PR, uncommitted, volume-in-use, root files, PR unreachable) | Keep as apply                                                      |      |
| Idempotent cleanup      | Safe to re-run; reports already-absent resources                                               | K                                                                  |      |
| Makefile clean targets  | image / volume / all-volumes / dind-data / venv / all                                          | K                                                                  |      |

## 13. Setup / deployment

| Capability                        | What it does                               | K/C/D                                                      | Hint                                      |
| --------------------------------- | ------------------------------------------ | ---------------------------------------------------------- | ----------------------------------------- |
| `make sync` / `build` / `rebuild` | Host venv + Docker image (UID/GID matched) | K - likely add targets for workflow & repo specific images |                                           |
| `make shell` / `info`             | Debug shell; image+volume status           | D                                                          |                                           |
| `make test`                       | pytest suite                               | K                                                          | Keep test discipline (ADR-driven harness) |

## 14. Naming conventions

| Capability                                               | What it does               | K/C/D | Hint                          |
| -------------------------------------------------------- | -------------------------- | ----- | ----------------------------- |
| Task id `YYYY-MM-DD-<slug>`                              | Stable id from date + slug | K     |                               |
| Branch `cloude/<slug>`                                   |                            | C     | rename → `panopticon/<slug>`? |
| tmux `cloude-<slug>`, image `cloude`, volumes `cloude-*` | Naming scheme              | C     | rename for panopticon         |

---

## Notes for categorization

- **Lifecycle-specific rows (§1, §6, parts of §2–3)** are the natural "Change" cluster:
  Milestone 1 turns the one hardcoded flow into a configurable workflow, so these become
  *one workflow definition* rather than hardcoded states/commands.
- **Storage rows (§5)** are mostly "Change" per ADR 0001 (DB) + ADR 0003 (plan/notes as
  file artifacts) — the question per row is DB-state vs. file-artifact vs. drop.
- **Container/integration rows (§7–8)** are largely "Keep" for Milestone 1, but §7 is the
  seam that Milestone 5 (remote execution) and Milestone 3 (other CLIs) will later push
  behind interfaces.
- **Secrets/repo rows (§9)** are "Keep + generalize" — already per-repo, which is exactly
  the Milestone 1 secrets model.
