# Container authentication — giving tasks a `claude` token

Every task runs `claude` inside its container. The agent authenticates from a
**`CLAUDE_CODE_OAUTH_TOKEN`** environment variable, which the runner injects from the **repo's
`env_file`** at spawn (ADR 0007 / ADR 0012). You provide that token once per repo; it is long-lived
and non-rotating, so it survives concurrent tasks and respawns (no ~8h re-login cliff).

You can do this by hand (mint with the `claude` CLI, drop the token into the env-file — below), or
run the **`setup-repo` workflow**, which does both on the host for you (see *The `setup-repo`
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

To skip the manual copy, enable the **`setup-repo`** workflow on the repo and start a task on it.
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
