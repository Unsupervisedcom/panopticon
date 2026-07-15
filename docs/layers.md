# Container image layers — how they work and how to add your own

Every task runs its agent inside a container. That container's image is not one fixed blob — it is
**composed from three layers** stacked in order: **base → workflow → repo**. The base stays small
and general; a workflow adds what its skills need; a repo adds its own toolchain. This doc explains
the model and gives copy-pasteable recipes for adding a workflow layer and a repo layer.

## The three tiers

Each tier is a Dockerfile fragment, and the effective image is those fragments `FROM`-chained on top
of one another:

- **Base** — minimal and general: `python`, `git`, `bash`, `curl`, `tmux`; the `claude` CLI the
  agent execs; the `panopticon` package; and the entrypoint (uid/gid remap → privilege drop). Built
  from `src/panopticon/docker/Dockerfile` and tagged `panopticon-base` (`make build`). It is
  deliberately **not** opinionated about any workflow or repo.
- **Workflow** — a fragment a `Workflow` subclass contributes via its `image_layer()` method, adding
  what the workflow's skills need. The GitHub-forge workflows layer in `gh`, for example, because
  their `open-pr`/`babysit-ci`/`babysit-merge` skills shell out to it.
- **Repo** — a fragment referenced by a repo's `image_layer_file`, adding repo-specific setup:
  build toolchain, dependencies, pre-launch configuration (e.g. `uv`, `make sync`).

The repo layer sits on top because it is the most specific and most frequently changing.

## How composition works

When the session service (the runner) spawns a task, it composes the image before `docker run`
(`sessionservice/spawner.py` `_compose_image`, `sessionservice/images.py`):

1. It fetches the two layers over REST from the task service —
   `GET /workflows/{name}/image-layer` and `GET /repos/{id}/image-layer` — and drops any that are
   empty.
2. If none remain, the task runs on **`panopticon-base`** directly (no build).
3. Otherwise it writes a Dockerfile that starts `FROM panopticon-base` and appends the fragments,
   then `docker build`s it, tagged **`panopticon-<workflow>-<repo_id>`**.

Docker layer-caches the result, so a spawn whose layers haven't changed rebuilds nothing — it's a
no-op once built. Change a layer and the next spawn rebuilds only the affected steps.

> Workflows whose `runner_type` is `"shell"` (e.g. `setup-repo`) run on the host with no container,
> so they have no image and layers are ignored.

## Adding a workflow layer

Override `image_layer()` on your `Workflow` subclass to return a Dockerfile fragment string. The
default (`core/workflow.py`) returns `""` — no layer. Everything the string contains is baked into
the image, so use it for system-level installs your skills depend on. The real example, from
`src/panopticon/workflows/github_forge.py`:

```python
def image_layer(self) -> str:
    """The forge skills shell out to `gh`, so layer it onto the base image."""
    return "RUN apt-get update && apt-get install --yes --no-install-recommends gh"
```

Notes:

- This is distinct from `tools()` and `skills()`. `image_layer()` puts a binary **in the image**;
  `tools()` just *names* it so the agent reaches for it, and `skills()` declares agent-driven
  procedures. Installing `gh` and naming it are two separate declarations on the same workflow.
- Spell external-program flags in full (`apt-get install --yes`, `--no-install-recommends`) — they
  are self-documenting and grep-able.
- Keep it small. The base stays general on purpose; only add what the workflow's own skills need.

## Adding a repo layer

A repo layer is **operator-authored** and referenced by name, so you don't touch code:

1. **Write the fragment file** under the layers directory —
   `~/.config/panopticon/layers/` (`$PANOPTICON_CONFIG/layers`; `core/dirs.py` `LAYERS_DIR`). For
   example `~/.config/panopticon/layers/myrepo.dockerfile`:

   ```dockerfile
   # Layered on top of base → workflow, so gh (etc.) is already present.
   RUN curl --location --silent https://astral.sh/uv/install.sh | sh
   ```

   It's a plain Dockerfile fragment — `RUN`/`ENV`/`COPY` lines, no `FROM` (the composer supplies
   that). It builds on top of the workflow layer as the unprivileged `panopticon` user's
   environment.

2. **Point the repo at it** in the dashboard's repo form: set `image_layer_file` to the file's
   **name** (`myrepo.dockerfile`), not a path. The form offers a picker over the files in your
   layers dir, with a custom-path entry that normalizes to a name.

The value is a **reference**, not inline content — see the `image_layer_file` field in
[`repos.md`](repos.md). The task service resolves it against its layers dir and serves the content
over REST; the runner composes it onto base → workflow. Names that escape the layers root (`..`,
absolute paths) are rejected; nested names (`team/myrepo.dockerfile`) are allowed. An empty or unset
`image_layer_file` means no repo layer.

## Notes

- **The layer file lives on the host that spawns the container.** Like `env_file` (see
  `docs/auth.md`), `image_layer_file` is a bare name resolved against each runner host's
  own layers dir. With a single host that's your machine; with remote runners (M5), place a
  same-named file under each runner host's layers dir.
- **Rebuilds & cleanup.** Editing a layer rebuilds the affected steps on the next spawn (Docker
  caches the rest). `make clean` removes `panopticon-base` and every composed `panopticon-*` image;
  `make build` rebuilds the base.
- **Elevated privileges are a capability, not a layer.** Docker-in-Docker is opt-in via the repo's
  `capabilities` map (`docker_in_docker`), which makes the runner spawn `--privileged` and the
  entrypoint start a nested daemon — it is not something you add through a Dockerfile fragment.
- **Secrets never go in a layer.** Image layers are cached and shared; keep API keys and tokens in
  the repo's `env_file` (`docs/auth.md`), injected at launch, not baked into the image.
