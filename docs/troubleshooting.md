# Troubleshooting — common first-run failures

Quick answers to the problems new installs hit most. Each entry points to the doc that owns
the detail; start there if the short answer isn't enough. For a fast health check of your host,
run `panopticon doctor` (see [reading `panopticon doctor`](#how-do-i-read-panopticon-doctor)).

### Quickstart stops at a Claude login, or I don't have a paid account

`claude setup-token` mints the agent's auth token through a browser OAuth flow, which needs a
**paid Claude subscription or Console login**. If you don't have one, put an `ANTHROPIC_API_KEY`
in the repo's env-file instead — it overrides the OAuth token and authenticates the same
containers.

→ [`auth.md`](auth.md#one-time-setup-per-account), and the API-key note in
[`auth.md`](auth.md#notes).

### My GitHub task can't open a PR, or `gh` fails

The container's `gh` needs a `GH_TOKEN`. Add one to the repo's env-file (the same file that
holds the Claude token) and respawn the task to pick it up.

→ [`repos.md`](repos.md#secrets-env_file) for the env-file, [`auth.md`](auth.md) for how it's
injected.

### The workflow I want isn't in the picker

The change-shipping workflows (`github-peer-reviewed`, `github-self-reviewed`,
`local-git-self-reviewed`) are **opt-in**. `quickstart` enables the one that matches your repo;
enable any others per repo in the repos form — press `g`, edit the repo, and check the
workflows you want. `spike` is always available.

→ [`workflows/README.md`](workflows/README.md#how-workflows-are-offered),
[`repos.md`](repos.md#workflow-visibility).

### A task is stuck at `awaiting`, or the container never reaches `live`

The runner can't finish spawning. Check that the **Docker daemon is running** and the **base
image is built** (`panopticon doctor` reports the daemon; `make build` builds the base image).
A spawn step that raised shows as `failed` with a detail string — fix the cause, then respawn
with `R`.

→ [`container.md`](container.md#when-it-goes-wrong).

### The dashboard and services seem gone (`tmux ls` shows nothing)

They're not on your default tmux server — they live on the dedicated `tmux -L panopticon`
server. Bring them back with `panopticon start`; if they're already up, `panopticon console`
re-attaches.

→ [README, Managing your install](../README.md#managing-your-install).

### The agent can't authenticate, or 401s mid-task

The token in the repo's env-file is missing, expired, or revoked. Mint a fresh one — re-run the
`setup-repo` task (repos form: `g`, highlight the repo, `s`) or overwrite the
`CLAUDE_CODE_OAUTH_TOKEN` line by hand — then respawn the task with `R` so the container picks
up the new value. A live task keeps its old token until it respawns.

→ [`auth.md`](auth.md#the-setup-repo-workflow), and the rotate/respawn note in
[`auth.md`](auth.md#notes).

### How do I read `panopticon doctor`?

It prints one line per prerequisite check — Python, Docker (and a running daemon), tmux, git,
and the `claude` CLI — marking each pass or fail, and exits non-zero if anything is missing.
Fix whatever's marked failed and re-run it.

→ [README, Requirements](../README.md#requirements).
