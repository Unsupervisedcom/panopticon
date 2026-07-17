# Repos — the repositories tasks operate on

Every task runs against a **repo**: the git repository it clones, works in, and (for the
GitHub workflows) opens a PR against. A repo is more than a git URL, though — it also carries
the per-repo configuration the runner needs to build and launch a task container: which secrets
to inject, what to add to the container image, which container privileges to grant, and which
workflows the repo offers.

Crucially, a repo record holds mostly **references**, never the sensitive values themselves. The
secrets, image layer, and hook script live as files on each runner host; the repo only names
them. That keeps secrets out of the database, artifacts, and image layers, and lets a remote
runner resolve each reference against its **own** host — so the value stays host-agnostic and
never crosses the wire.

A repo's identity is its `id`; `name` is the human label. Every task carries a `repo_id`
pointing at one.

## Fields

The `Repo` model (`src/panopticon/core/models.py`) has these fields:

| Field | Meaning |
|---|---|
| `id` | Stable identifier; a task references it as `repo_id`. |
| `name` | Human-readable label. |
| `git_url` | Git remote the per-task clone is cloned from and `origin` points at. |
| `default_base` | Default base branch for new tasks (defaults to `main`). |
| `env_file` | **Name** (relative to the secrets dir) of an env-file of secrets, injected at spawn via `--env-file`. See [Secrets](#secrets-env_file). |
| `image_layer_file` | **Name** (relative to the layers dir) of a Dockerfile fragment — the repo tier of the composed image. See [Container image](#container-image-image_layer_file). |
| `capabilities` | Opt-in map for elevated container privileges (e.g. `docker_in_docker`). See [Capabilities](#capabilities). |
| `hook_file` | **Name** (relative to the hooks dir) of a script the runner runs before `docker run`. See [Host hook](#host-hook-hook_file). |
| `enabled_workflows` / `disabled_workflows` | Filter which workflows the repo offers. See [Workflow visibility](#workflow-visibility). |

The three reference fields (`env_file`, `image_layer_file`, `hook_file`) are all optional — a
minimal repo is just an `id`, `name`, and `git_url`.

## Secrets (`env_file`)

`env_file` is a **name relative to the secrets dir** — `$PANOPTICON_CONFIG/secrets`, default
`~/.config/panopticon/secrets/` — naming a file of `KEY=value` lines. At spawn the runner
resolves the name against its own host's secrets dir and injects the file with
`docker run --env-file`, so the task container gets exactly its repo's secrets and nothing
else.

Because only the *name* is stored, secrets stay out of the database, artifacts, and image
layers, and a remote runner resolves the same name against its own host — the file's content
never crosses the wire. The resolver refuses any name that escapes the secrets dir (a `..`
segment or an absolute path).

The env-file's most important entry is the container's `claude` auth token,
`CLAUDE_CODE_OAUTH_TOKEN` (plus any `ANTHROPIC_API_KEY` or `GH_TOKEN` the tasks need). You don't
have to write it by hand: **`panopticon quickstart` sets a repo's token up at any time**, and the
[`setup-repo` workflow](workflows/setup-repo.md) is available from the dashboard whenever you need
to (re-)mint it — press `g` to open the repos modal, highlight the repo, and press `s`. See
[`auth.md`](auth.md) for the details and the by-hand path.

`env_file` is validated at create time: `POST /repos` rejects a reference whose file doesn't
exist under the secrets dir.

## Container image (`image_layer_file`)

`image_layer_file` is a **name relative to the layers dir**, naming a Dockerfile fragment (not
inline content) — the repo's own tier of the task image, which the runner composes as
**base → workflow → repo** and builds at spawn. This is where a repo layers on its toolchain,
for example installing `uv` and `make`. The task service serves the fragment over
`GET /repos/{id}/image-layer`; the layer is optional (declare none and the repo tier is empty),
and secrets are never baked in — they're injected at run time via `env_file`. See
[`layers.md`](layers.md) for how the layers compose.

## Capabilities

`capabilities` is a JSON opt-in map for elevated container privileges the runner grants at
spawn. The first (and currently only) capability is `docker_in_docker`: set it and the runner
spawns the container `--privileged`, gives it a volume for `/var/lib/docker`, and the entrypoint
starts a nested Docker daemon. It's **off by default** because it's a trust escalation — a
privileged container is effectively host root — so a repo opts in only when its tasks genuinely
need to run Docker.

## Host hook (`hook_file`)

`hook_file` names a script the runner runs on the host after the per-task workspace is prepared
but before `docker run` — a chance to adjust the checkout before the agent sees it (for example
stripping host-only config files). Like `env_file`, it is a **name relative to the hooks dir**
(`$PANOPTICON_CONFIG/hooks`), resolved against each runner's own host. See
[`hooks.md`](hooks.md) for what the hook receives and how failures are handled.

## Workflow visibility

`enabled_workflows` and `disabled_workflows` filter which workflows the repo offers in the
task-creation picker, on top of each workflow's own opt-in flag. `GET /repos/{id}/workflows`
returns the filtered list.

## How a task uses its repo

When the session service spawns a task, it uses the repo's fields in order:

1. **Fetch the repo** by the task's `repo_id`.
2. **Prepare the per-task clone** — `git clone --local` from the host's per-repo cache into a
   fresh directory mounted read-write at `/workspace`.
3. **Run `hook_file`** on the host, if the repo declares one.
4. **Compose the image** — fetch the workflow and repo (`image_layer_file`) layers, build
   base → workflow → repo.
5. **`docker run`** the container with the repo's config: `--env-file` from `env_file`, the
   `/workspace` mount, and `--privileged` when `capabilities.docker_in_docker` is set.

## Managing repos

Repos are managed over the task service's REST API:

- `POST /repos` — create a repo (validates that `env_file` exists).
- `GET /repos` / `GET /repos/{id}` — list or fetch.
- `PATCH /repos/{id}` — partial update; fields you don't send are preserved.
- `GET /repos/{id}/workflows` — the workflows this repo offers.
- `GET /repos/{id}/image-layer` — the repo's composed Dockerfile layer.

## Related

- [`auth.md`](auth.md) — the `claude` token that lives in `env_file`.
- [`layers.md`](layers.md) — how the base → workflow → repo image layers compose.
- [`hooks.md`](hooks.md) — repo hooks and how `hook_file` resolves.
- [`workflows/setup-repo.md`](workflows/setup-repo.md) — host-side utility that mints and places the auth token.
