# The `github-peer-reviewed` workflow

This task moves through a fixed sequence of phases. You are always in exactly one phase: do that phase's work, then it advances. Each turn you'll be reminded which phase you're in and what it needs — **don't do a later phase's work early.** The phases, in order:

1. **PLANNING** — you finish its responsibilities, then hand back to the user, who advances it:
   - plan-written: The plan is written into the plan artifact.
2. **ITERATING** — you finish its responsibilities, then hand back to the user, who advances it:
   - plan-implemented: The plan is implemented in code.
   - requests-implemented: All user requests are implemented in code.
   - tests-pass: New and relevant tests pass locally.
   - committed-pushed: Changes are committed and pushed.
   - ci-passing: CI tests are passing, or any failures are irrelevant flakes.
   - pr-updated: The PR title and description reflect the final change, with no Test Plan / Verification section.
3. **REVIEW** — you finish its responsibilities, then hand back to the user, who advances it:
   - pr-reviewed: The PR has been reviewed.
4. **MERGING** — you advance it yourself once its responsibilities are met:
   - pr-merged: The PR is merged.
5. **COMPLETE** — terminal; the task is finished.

Moving between phases: **`advance`** follows this sequence and is gated on the current phase's responsibilities; **`drop`** abandons the task (→ DROPPED) from anywhere; and if the user redirects you, you can move straight to any phase (a free move — e.g. back to an earlier phase to redo work).

## Tools

Beyond the usual shell (git, bash, …), this workflow's container has:
- `gh` — the GitHub CLI — authenticated to the forge. Use it for all remote VCS: open and update the PR (`gh pr ...`), watch CI (`gh pr checks`), and merge. The forge skills drive it.