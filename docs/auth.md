# Container authentication — giving a task's agent CLI its credentials

Every task runs an agent CLI inside its container — **`claude` by default, or `codex`** when the
repo's `agent_cli` selects it. Either way the agent authenticates from credentials the runner injects
from the **repo's `env_file`** at spawn (ADR 0007 / ADR 0012); which variable(s) you set depends on
the CLI. This page covers **claude** first (the default), then **codex** — tasks whose `agent_cli`
is `codex`.

## Claude (the default)

Claude authenticates from a **`CLAUDE_CODE_OAUTH_TOKEN`** environment variable, which the runner
injects from the repo's `env_file`. You provide that token once per repo; it is long-lived and
non-rotating, so it survives concurrent tasks and respawns (no ~8h re-login cliff).

Normally you don't set this up by hand: **`panopticon quickstart` registers the repo and drops you
into a `setup-repo` task** that mints the token and writes it into the env-file for you. This section
is the deep-dive and the manual path — set it up by hand (mint with the `claude` CLI, drop the token
into the env-file — below), or run the **`setup-repo` workflow** on its own (see *The `setup-repo`
workflow* below). There is no `login` command.

### One-time setup per account

1. **Mint a long-lived token** on a machine where you can complete the browser OAuth (it needs a
   Claude subscription or Console login):

   ```sh
   claude setup-token
   ```

   Complete the browser flow; the command prints a token (`sk-ant-oat01-…`). It's long-lived
   (~1 year), non-rotating, and inference-only — exactly what an unattended container needs. The
   same token works for every repo; minting another does not invalidate it, so you can roll out a
   renewal gradually.

2. **Add it to the repo's env-file.** Each repo has an `env_file` — a **name relative to the secrets
   dir** (`~/.config/panopticon/secrets/`, or `$PANOPTICON_CONFIG/secrets`) naming a file of
   `KEY=value` lines that the runner injects into the task container (`--env-file`). Add (or update)
   one line:

   ```sh
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
   ```

   Keep the file `0600` and out of version control. If the repo has no `env_file` yet, create one
   under the secrets dir (e.g. `~/.config/panopticon/secrets/<repo>.env`) and set the repo's
   `env_file` to its **name** (`<repo>.env`) — in the dashboard's repo form (which accepts an
   absolute or relative path and normalizes it to a name), or via the API:

   ```sh
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"env_file": "<repo>.env"}'
   ```

That's it — new task containers for that repo now authenticate from the token.

### The `setup-repo` workflow

`panopticon quickstart` runs this workflow for you. To do it manually, start a **`setup-repo`** task
from the repos modal — press `g` on the dashboard, highlight the repo, and press `s`.
It runs on the host (no container — `runner_type = "shell"`), attaches you to a shell where it runs
`claude setup-token`, and on a successful mint **writes the token straight into the repo's env-file**
as `CLAUDE_CODE_OAUTH_TOKEN=…` (creating the file `0600` if needed). If a token is already present,
the previous line is **commented out** (kept as a record, not deleted) and any placeholder stub
(`# CLAUDE_CODE_OAUTH_TOKEN =`) is removed; other lines (`ANTHROPIC_API_KEY`, …) are untouched. When
it can't capture the token (or the repo has no `env_file`), it falls back to printing the copy-it-in
instructions above.

## Codex (tasks whose `agent_cli` is `codex`)

A repo whose `agent_cli` is `codex` runs the `codex` CLI in its task containers instead of `claude`,
and codex authenticates differently: it reads credentials from `$CODEX_HOME/auth.json`, not from an
env var directly, and otherwise reaches for an OS keyring the container doesn't have. The container
adapter bridges this — it pins codex to the **file** credential store and, on the container's first
launch, materializes `auth.json` from whatever key you put in the repo's env-file.

So setup is the same shape as claude's: **add one line to the repo's `env_file`** (see *Add it to the
repo's env-file* above for how to create one and point the repo at it). Choose **one**:

- **API key** — `OPENAI_API_KEY=sk-…` (or `CODEX_API_KEY=sk-…`; both spellings are accepted). The
  standard API-billed key from platform.openai.com. On first launch the adapter writes
  `$CODEX_HOME/auth.json` as `{"auth_mode": "apikey", "OPENAI_API_KEY": "…"}` (mode `0600`).
- **ChatGPT workspace token** — `CODEX_ACCESS_TOKEN=…`, a ChatGPT Business/Enterprise workspace
  access token (minted at chatgpt.com/admin → access tokens), the analog of `claude setup-token`.
  Codex reads it straight from the env; no file is written.

That's it — new codex task containers for that repo now authenticate from the env-file. There is no
`setup-repo` equivalent for codex yet, so add the line by hand.

Notes specific to codex:

- **Idempotent, never clobbered.** If `auth.json` already exists on the per-task config volume (a
  container already logged in, e.g. carried across respawn), the adapter leaves it untouched — and a
  container that has only a persisted `auth.json` still counts as authenticated.
- **No validation up front.** We only check that a credential is *present*; an invalid or expired key
  surfaces at codex's first call, not at launch.
- **Rotating an API key.** Because `auth.json` lives on the per-task config volume and is written
  once (never clobbered), changing `OPENAI_API_KEY` in the env-file and respawning **won't** re-auth
  an existing task — its `auth.json` is already there. New tasks pick up the new key; to rotate a
  live one, clear its `auth.json` from the per-task volume before respawn. (`CODEX_ACCESS_TOKEN`,
  read from the env, has no such caching — respawn picks up a change.)

## Notes (both CLIs)

- **The env-file lives on the host that spawns the container.** Because `env_file` is stored as a
  bare name resolved against each runner's own `~/.config/panopticon/secrets/`, the same repo record
  works across hosts: with a single host (M1) that's the machine you minted on; with remote runners
  (M5), place a same-named env-file under each runner host's secrets dir.
- **`ANTHROPIC_API_KEY` overrides `CLAUDE_CODE_OAUTH_TOKEN` (claude).** If a repo needs to burst past
  the subscription rate limit, put an `ANTHROPIC_API_KEY` in the same env-file — but don't set both
  unintentionally, since the API key wins.
- **Already-running tasks** keep their old credentials until they respawn. After editing the
  env-file, respawn a live task from the dashboard (`R`) to pick up the new value (for codex, mind
  the `auth.json` caching noted above).
- **Rotating/revoking (claude).** To replace a token, mint a new one and overwrite the env-file line
  (or re-run the `setup-repo` workflow, which comments out the old line and appends the new one).
  Per-token revocation isn't available upstream (account-level "revoke all" can take time to
  propagate), so treat a leak as "mint a replacement + monitor usage in the Console," and keep the
  env-file tightly held.
