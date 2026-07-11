# Container authentication — giving tasks a `claude` token

Every task runs `claude` inside its container. The agent authenticates from a
**`CLAUDE_CODE_OAUTH_TOKEN`** environment variable, which the runner injects from the **repo's
`env_file`** at spawn (ADR 0007 / ADR 0012). You provide that token once per repo; it is long-lived
and non-rotating, so it survives concurrent tasks and respawns (no ~8h re-login cliff).

panopticon does **not** mint or store the token for you — there is no `login` command. You obtain it
with the `claude` CLI and place it in the repo's env-file yourself.

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

2. **Add it to the repo's env-file.** Each repo has an `env_file` — a host path to a file of
   `KEY=value` lines that the runner injects into the task container (`--env-file`). Add (or update)
   one line:

   ```sh
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
   ```

   Keep the file `0600` and out of version control. If the repo has no `env_file` yet, create one
   (e.g. `~/.config/panopticon/secrets/<repo>.env`) and set the repo's `env_file` to that path — in
   the dashboard's repo form, or via the API:

   ```sh
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"env_file": "/home/you/.config/panopticon/secrets/<repo>.env"}'
   ```

That's it — new task containers for that repo now authenticate from the token.

## Notes

- **The env-file lives on the host that spawns the container.** With a single host (M1) that's the
  same machine you minted on. With remote runners (M5), distribute the env-file to each runner host
  alongside the repo's other secrets.
- **`ANTHROPIC_API_KEY` overrides `CLAUDE_CODE_OAUTH_TOKEN`.** If a repo needs to burst past the
  subscription rate limit, put an `ANTHROPIC_API_KEY` in the same env-file — but don't set both
  unintentionally, since the API key wins.
- **Already-running tasks** keep their old token until they respawn. After editing the env-file,
  respawn a live task from the dashboard (`R`) to pick up the new value.
- **Rotating/revoking.** To replace a token, mint a new one and overwrite the env-file line.
  Per-token revocation isn't available upstream (account-level "revoke all" can take time to
  propagate), so treat a leak as "mint a replacement + monitor usage in the Console," and keep the
  env-file tightly held.
