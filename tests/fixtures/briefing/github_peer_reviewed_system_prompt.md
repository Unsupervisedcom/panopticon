# The `github-peer-reviewed` workflow

This task moves through a fixed sequence of phases. You are always in exactly one phase: do that phase's work, then it advances. Each turn you'll be reminded which phase you're in and what it needs — **don't do a later phase's work early.** The phases, in order:

1. **PLANNING** — Collect requirements. Produce a plan for the implementation. You must meet these responsibilities before ending your turn:
   - plan-written: The plan is written to the plan artifact as a markdown file (`plan.md`).
   The user will advance to the next state.
2. **ITERATING** — Implement the plan. Implement any additional user requests or feedback. Implement any review comments the user has approved for implementation. You must meet these responsibilities before ending your turn:
   - plan-implemented: The plan is implemented in code.
   - requests-implemented: All user requests are implemented in code.
   - tests-pass: New and relevant tests pass locally.
   - committed-pushed: Changes are committed and pushed.
   - ci-passing: CI tests are passing, or any failures are irrelevant flakes.
   - pr-updated: The PR title and description reflect the final change, with no Test Plan / Verification section.
   The user will advance to the next state.
3. **REVIEW** — Wait for review or approval of the PR. You must meet these responsibilities before ending your turn:
   - pr-reviewed: The PR has been reviewed.
   The user will advance to the next state.
4. **MERGING** — Add the PR to the merge queue. If the PR exits the merge queue, re-add it. You must meet these responsibilities before ending your turn:
   - pr-merged: The PR is merged.
   Automatically advance to the next state.
5. **COMPLETE** — terminal. The work has landed; the task is finished.

Moving between phases: **`advance`** follows this sequence and is gated on the current phase's responsibilities; **`drop`** abandons the task (→ DROPPED) from anywhere; and if the user redirects you, you can move straight to any phase (a free move — e.g. back to an earlier phase to redo work).

## Tools

Beyond the usual shell (git, bash, …), this workflow's container has:
- `gh` — the GitHub CLI — authenticated to the forge. Use it for all remote VCS: open and update the PR (`gh pr ...`), watch CI (`gh pr checks`), and merge. The forge skills drive it.