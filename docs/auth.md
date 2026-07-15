# Container authentication — giving tasks their agent credentials

Each **harness** (the agent CLI a task runs — claude by default, codex for OpenAI models)
authenticates its own way. The claude setup is below; codex follows in
[Codex / OpenAI](#codex--openai-gpt-56).

Every task runs `claude` inside its container. The agent authenticates from a
**`CLAUDE_CODE_OAUTH_TOKEN`** environment variable, which the runner injects from the **repo's
`env_file`** at spawn (ADR 0007 / ADR 0012). You provide that token once per repo; it is long-lived
and non-rotating, so it survives concurrent tasks and respawns (no ~8h re-login cliff).

Normally you don't set this up by hand: **`panopticon quickstart` registers the repo and drops you
into a `setup-repo` task** that mints the token and writes it into the env-file for you. This page is
the deep-dive and the manual path — set it up by hand (mint with the `claude` CLI, drop the token
into the env-file — below), or run the **`setup-repo` workflow** on its own (see *The `setup-repo`
workflow* below). There is no `login` command.

## One-time setup per account

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

## The `setup-repo` workflow

`panopticon quickstart` runs this workflow for you. To do it manually, start a **`setup-repo`** task
from the repos modal — press `g` on the dashboard, highlight the repo, and press `s`.
It runs on the host (no container — `runner_type = "shell"`), attaches you to a shell where it runs
`claude setup-token`, and on a successful mint **writes the token straight into the repo's env-file**
as `CLAUDE_CODE_OAUTH_TOKEN=…` (creating the file `0600` if needed). If a token is already present,
the previous line is **commented out** (kept as a record, not deleted) and any placeholder stub
(`# CLAUDE_CODE_OAUTH_TOKEN =`) is removed; other lines (`ANTHROPIC_API_KEY`, …) are untouched. When
it can't capture the token (or the repo has no `env_file`), it falls back to printing the copy-it-in
instructions above.

## Notes

- **The env-file lives on the host that spawns the container.** Because `env_file` is stored as a
  bare name resolved against each runner's own `~/.config/panopticon/secrets/`, the same repo record
  works across hosts: with a single host (M1) that's the machine you minted on; with remote runners
  (M5), place a same-named env-file under each runner host's secrets dir.
- **`ANTHROPIC_API_KEY` overrides `CLAUDE_CODE_OAUTH_TOKEN`.** If a repo needs to burst past the
  subscription rate limit, put an `ANTHROPIC_API_KEY` in the same env-file — but don't set both
  unintentionally, since the API key wins.
- **Already-running tasks** keep their old token until they respawn. After editing the env-file,
  respawn a live task from the dashboard (`R`) to pick up the new value.
- **Rotating/revoking.** To replace a token, mint a new one and overwrite the env-file line (or
  re-run the `setup-repo` workflow, which comments out the old line and appends the new one).
  Per-token revocation isn't available upstream (account-level "revoke all" can take time to
  propagate), so treat a leak as "mint a replacement + monitor usage in the Console," and keep the
  env-file tightly held.

## Codex / OpenAI (GPT-5.6)

A task created with `harness: "codex"` runs OpenAI's Codex CLI in its container. Three
credential tiers, in order of setup effort:

1. **API key** (pay-per-token): add one line to the repo's env-file —

   ```sh
   CODEX_API_KEY=sk-...
   ```

   The harness renders it into codex's `auth.json` at container start (the same shape
   `codex login --with-api-key` writes). `OPENAI_API_KEY` works too.

2. **ChatGPT Business/Enterprise access token** (non-rotating — the exact analog of
   `claude setup-token`): mint at `chatgpt.com/admin/access-tokens`, then

   ```sh
   CODEX_ACCESS_TOKEN=...
   ```

   in the env-file. Codex reads it straight from the environment.

3. **ChatGPT Plus/Pro subscription** (rotating tokens — needs the shared credential dir):

   ```sh
   # on the host, once per account:
   codex login              # or: codex login --device-auth (headless)
   mkdir -p ~/.config/panopticon/secrets/openai.d
   cp ~/.codex/auth.json ~/.config/panopticon/secrets/openai.d/
   chmod 0600 ~/.config/panopticon/secrets/openai.d/auth.json
   # then point the repo at it:
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"credential_dir": "openai.d"}'
   ```

   The runner mounts the dir **read-write and shared** into that repo's task containers; the
   harness symlinks `auth.json` into each task's `CODEX_HOME`. Sharing is deliberate: ChatGPT
   refresh tokens **rotate with reuse detection**, so every session must converge on one copy —
   codex reloads the file from disk before refreshing (and on 401) and writes refreshed tokens
   back through the symlink, so concurrent sessions on one host stay consistent. Do **not**
   copy the same auth.json to a second host (OpenAI's documented constraint); log in per host,
   or use an access token. If the chain is ever invalidated (re-login elsewhere, revocation),
   tasks fail with a lifecycle detail naming the fix — re-run the login + copy above.

Pick the model per task via `starting_model` (e.g. `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`). Note the fleet-level constraint: plan rate limits (not auth) cap concurrent
Codex throughput on Plus/Pro.
