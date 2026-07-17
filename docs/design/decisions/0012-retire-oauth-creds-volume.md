# 0012 — Retire the OAuth creds volume; container auth via a setup-token in the env-file

- Status: Accepted
- Date: 2026-06-25
- Deciders: Charlie Scherer
- Amends: 0007 (per-repo secrets)

## Context

ADR 0007 gave a repo two secret mechanisms, both injected at container launch: an **env-file** of
API-key-style secrets (`--env-file`), and a per-repo **OAuth credential volume** (the cloude-cade
`…-creds` volume, mounted at `/creds`). The agent launcher symlinked `.credentials.json` from that
volume into the container-local config and seeded the logged-in account, so a task authenticated as
the repo's subscription login. `panopticon login <repo>` populated the volume interactively.

That credential volume is unworkable for concurrent task containers. Claude's OAuth **refresh token
is single-use/rotating**: the first container to refresh the ~8h access token invalidates the copy
every *other* container — and every respawn — still holds, forcing a re-login at the ~8h cliff. The
refresh is an atomic temp+rename that replaces our symlink with a container-local regular file, so
the rotated token never writes back to `/creds`; the next spawn re-links the now-dead volume token.
Net: container auth dies within ~8h while the host's single-consumer login lasts indefinitely.

## Decision

**Retire the per-repo OAuth creds volume entirely.** Container `claude` auth is now a **non-rotating
`claude setup-token`** that the operator places in the repo's existing **`env_file`** as
`CLAUDE_CODE_OAUTH_TOKEN=…`. The runner already injects `env_file` via `--env-file` at spawn, so the
token reaches every container with no new mechanism; `claude` reads it straight from the environment
— no `/creds` mount, no symlink, no account seed. A single static token authenticates every
container and respawn, so there is no refresh race and no 8h cliff.

**panopticon does not mint or store the token.** There is **no `panopticon login` command** and no
credential-management surface in the control plane. Obtaining the token (`claude setup-token`) and
putting it in the repo's env-file is an **operator step**, documented in
[`docs/auth.md`](../auth.md). This keeps the control plane out of the
secret-custody business: as in ADR 0007, panopticon records only the env-file *reference*, never its
contents.

### What this removes

- The `creds_volume` field/column on `Repo` and the `/creds` mount (runner + base image entrypoint).
- `link_credentials` / `seed_account` and the account/creds constants in the agent launcher.
- The `panopticon login` CLI command, the dashboard repo-screen login action, the runner's
  interactive `login`, and the post-login container restart helper.

### What stays

- The repo **`env_file`** mechanism (ADR 0007), unchanged — it now also carries
  `CLAUDE_CODE_OAUTH_TOKEN` alongside any `ANTHROPIC_API_KEY`/`GH_TOKEN`.

## Consequences

- **Much simpler control plane** — no credential custody, no minting, no login flow. The token is a
  plain env var the operator manages like any other per-repo secret.
- **Host-locality (accepted).** `env_file` is a host path, so it must exist on every host that
  spawns the repo's containers, and the operator places it there. Fine for M1 (single host); at M5
  (remote runners) the operator distributes the env-file with the rest of the repo's secrets. We
  considered storing the token in the DB (encrypted) to make it travel with the control plane, but
  judged the crypto + key-management machinery not worth it versus the env-file the runner already
  injects.
- **Precedence.** `ANTHROPIC_API_KEY` (also in `env_file`) **overrides** `CLAUDE_CODE_OAUTH_TOKEN` —
  don't set both on one repo unintentionally. The API key remains the high-throughput escape hatch.
- **Token lifecycle.** The `setup-token` is long-lived (~1yr), non-rotating, account-level, and
  inference-only (no Remote Control). Renewal is manual and infrequent; a new token doesn't
  invalidate existing ones, so renewal can roll out gradually. Revocation is weak upstream (no
  per-token revoke; account-level "revoke all" can lag days) — treat a leak as "mint a replacement +
  monitor the Console," and keep the env-file `0600`.
- A running task picks up a changed token on its **next spawn** — respawn it (dashboard `R`) after
  editing the env-file.

## References

- Concurrent-refresh race / forced `/login`: claude-code issues #24317, #54443, #27933, #56339.
- No list/revoke tooling: #48373. Revoke-all leaves tokens valid for days: #43801.
- Docs: `claude setup-token`, `CLAUDE_CODE_OAUTH_TOKEN`, auth precedence (authentication.md).
- In-repo: [`docs/design/decisions/0007-secret-store.md`](0007-secret-store.md) (amended by this ADR);
  [`docs/auth.md`](../auth.md) (the operator how-to).
