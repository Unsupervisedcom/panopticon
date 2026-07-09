# panopticon — dev tasks. Thin wrappers over `uv`/`docker`; see CLAUDE.md for details.
.DEFAULT_GOAL := help
.PHONY: help sync test typecheck check serve dashboard host start stop build clean migrate migrate-revision

#: The base task-container image (ADR 0005 base layer); must match DEFAULT_IMAGE.
IMAGE ?= panopticon-base

help:  ## List available targets
	@grep -h -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | sort | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## Create the venv and install dependencies
	uv sync

test:  ## Run the test suite
	uv run pytest

typecheck:  ## Type-check (mypy, strict)
	uv run mypy --package panopticon

check: typecheck test  ## Type-check + tests (what CI runs)

migrate:  ## Apply DB migrations up to head (uses $PANOPTICON_DB; default ~/.local/share/panopticon/panopticon.db)
	uv run alembic --config src/panopticon/alembic.ini $(if $(DB),-x db=$(DB),) upgrade head

migrate-revision:  ## Autogenerate a migration from ORM changes (MSG="describe the change")
	uv run alembic --config src/panopticon/alembic.ini revision --autogenerate -m "$(MSG)"

serve:  ## Run the task service over HTTP (the control plane)
	uv run python -m panopticon.taskservice

dashboard:  ## Launch the dashboard (foreground; no tmux)
	uv run panopticon dashboard

host:  ## Start task service + session-service host in background tmux sessions (no console; use for CI or headless ops)
	uv run panopticon host

start:  ## Run panopticon: task service + session-service runner (background) + dashboard supervisor
	uv run panopticon start

stop:  ## Stop everything `make start` started: the task containers + the -L panopticon tmux server
	uv run panopticon stop

build:  ## Build the base task-container image (override with IMAGE=)
	uv build --wheel --out-dir src/panopticon/docker/
	docker build \
	  --tag $(IMAGE) \
	  --build-arg PANOPTICON_WHEEL=$(shell ls -1 src/panopticon/docker/panopticon_app*.whl | xargs -n1 basename) \
	  --file src/panopticon/docker/Dockerfile \
	  src/panopticon/docker/
	rm --force src/panopticon/docker/panopticon_app*.whl

clean:  ## Remove the base image and any composed panopticon-* images
	-docker rmi --force $(IMAGE)
	-docker images --quiet 'panopticon-*' | sort -u | { ids=$$(cat); [ -z "$$ids" ] || docker rmi --force $$ids; }
