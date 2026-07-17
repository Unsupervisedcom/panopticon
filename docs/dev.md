# Developing panopticon

Working *on* panopticon rather than running it? This is the core development loop: set up a
venv, run the checks, and (optionally) bring the stack up locally. Just want to *use*
panopticon? Start with the [README](../README.md). Want the full picture — the module map,
conventions, and the tests worth knowing — read [`AGENTS.md`](../AGENTS.md) and the design
docs under [`docs/design/`](design/README.md).

## Prerequisites

- **Python 3.11+**
- **[`uv`](https://docs.astral.sh/uv/)** — the package/venv manager (`brew install uv`, or the
  astral installer).

That's all you need to edit code and run the checks. **Running the whole stack** additionally
needs Docker, tmux, git, and the `claude` CLI — see the
[README requirements](../README.md#requirements) (and [`docs/macos-setup.md`](macos-setup.md)
on macOS).

A [`Makefile`](../Makefile) wraps the `uv` commands; `make help` lists every target.

## Setup

```sh
make sync        # uv sync — create the venv and install deps (incl. the dev group)
```

## The check loop

`make check` is the inner loop — it's exactly what CI runs, so if it's green locally the PR
gate will be too:

```sh
make check       # lint-check + typecheck + test
```

Run the pieces individually while iterating:

| Command | What it does |
|---|---|
| `make lint` | Ruff **lint + auto-format** (`ruff check --fix` then `ruff format`) — fixes in place |
| `make format` | Format only (`ruff format`) |
| `make typecheck` | `mypy --package panopticon` (strict) |
| `make test` | `pytest` |
| `make lint-check` | Lint + format **check, read-only** — the CI-parity gate `make check` uses |

The distinction that matters: `make lint` **modifies** your files (auto-fix + format), while
`make lint-check` only **reports** (it's what CI runs, so it never rewrites code). Run
`make lint` before committing to fix findings; `make check` to confirm you match CI.

Ruff owns line width via `ruff format`, so there's no separate line-length nag. The ruleset
lives under `[tool.ruff]` in [`pyproject.toml`](../pyproject.toml).

## Running the stack locally

To exercise the real system (not just the tests) you need the container toolchain from the
prerequisites above. Build the base image once, then bring everything up:

```sh
make build       # docker-build the base task-container image (needed before spawning tasks)
make start       # task service + session-service runner + dashboard supervisor
make stop        # tear it all down (task containers + the -L panopticon tmux server)
```

Individual pieces, when you want just one:

- `make serve` — the task service (control plane) over HTTP
- `make dashboard` — the dashboard once, foreground (no tmux)
- `make host` — task service + session-service host in background tmux (headless/CI)

See [`docs/overview.md`](overview.md) for the mental model behind these pieces, and the
[README quickstart](../README.md#quickstart) for the end-to-end first run.

## Database migrations

Schema is managed by **Alembic**. After changing the ORM rows, generate and apply a migration:

```sh
make migrate-revision MSG="describe the change"   # autogenerate from ORM changes
make migrate                                       # apply up to head
```

Commit the generated file under `src/panopticon/migrations/versions/`.
`tests/test_migrations.py` guards the migrations against drift from the ORM schema. See the
Dev-commands section of [`AGENTS.md`](../AGENTS.md) for the details.

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push to `main` and
every PR (on a Python 3.13 runner). It is the same sequence `make check` wraps:

```
uv sync
uv run ruff check           # lint
uv run ruff format --check  # format check
uv run mypy --package panopticon
uv run pytest
```

So `make check` locally reproduces the PR gate — get it green before you push.

## Where to go deeper

- [`AGENTS.md`](../AGENTS.md) — the operating manual: the determinism invariant, module map,
  conventions, dev commands, and the tests worth knowing.
- [`docs/design/`](design/README.md) — goals, architecture, roadmap, and the ADRs.
- [`docs/overview.md`](overview.md) — how the running pieces fit together.
