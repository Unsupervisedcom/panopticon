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
	uv run alembic $(if $(DB),-x db=$(DB),) upgrade head

migrate-revision:  ## Autogenerate a migration from ORM changes (MSG="describe the change")
	uv run alembic revision --autogenerate -m "$(MSG)"

serve:  ## Run the task service over HTTP (the control plane)
	uv run python -m panopticon.taskservice

dashboard:  ## Launch the dashboard (foreground; no tmux)
	uv run panopticon dashboard

host: migrate  ## Start task service + session-service host in background tmux sessions (no console; use for CI or headless ops)
	# Always kill-and-recreate so a crashed process doesn't leave a stale session that make host silently reuses.
	tmux -L panopticon kill-session -t service 2>/dev/null || true
	tmux -L panopticon new-session -d -s service 'uv run python -m panopticon.taskservice 2>&1 | tee /tmp/panopticon-service.log'
	tmux -L panopticon kill-session -t runner 2>/dev/null || true
	tmux -L panopticon new-session -d -s runner 'uv run python -m panopticon.sessionservice.host 2>&1 | tee /tmp/panopticon-runner.log'

start: host  ## Run panopticon: task service + session-service runner (background) + dashboard supervisor
	uv run panopticon console

stop:  ## Stop everything `make start` started: the task containers + the -L panopticon tmux server
	-docker ps --all --quiet --filter label=panopticon.task | { ids=$$(cat); [ -z "$$ids" ] || docker rm --force $$ids; }
	-tmux -L panopticon kill-server 2>/dev/null

build:  ## Build the base task-container image (override with IMAGE=)
	docker build --tag $(IMAGE) --file docker/Dockerfile .

clean:  ## Remove the base image and any composed panopticon-* images
	-docker rmi --force $(IMAGE)
	-docker images --quiet 'panopticon-*' | sort -u | { ids=$$(cat); [ -z "$$ids" ] || docker rmi --force $$ids; }
