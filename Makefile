# panopticon — dev tasks. Thin wrappers over `uv`/`docker`; see CLAUDE.md for details.
.DEFAULT_GOAL := help
.PHONY: help sync test typecheck check serve dashboard start stop build clean migrate migrate-revision

#: The base task-container image (ADR 0005 base layer); must match DEFAULT_IMAGE.
IMAGE ?= panopticon-base

help:  ## List available targets
	@grep --no-filename --extended-regexp '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | sort | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## Create the venv and install dependencies
	uv sync

test:  ## Run the test suite
	uv run pytest

typecheck:  ## Type-check (mypy, strict)
	uv run mypy --package panopticon

check: typecheck test  ## Type-check + tests (what CI runs)

migrate:  ## Apply DB migrations up to head (uses $PANOPTICON_DB; override with DB=<url>)
	uv run alembic $(if $(DB),-x db=$(DB),) upgrade head

migrate-revision:  ## Autogenerate a migration from ORM changes (MSG="describe the change")
	uv run alembic revision --autogenerate -m "$(MSG)"

serve:  ## Run the task service over HTTP (the control plane)
	uv run python -m panopticon.taskservice

dashboard:  ## Launch the dashboard (foreground; no tmux)
	uv run panopticon dashboard

start:  ## Run panopticon: task service + session-service runner (background) + dashboard supervisor
	tmux -L panopticon has-session -t service 2>/dev/null || \
		tmux -L panopticon new-session -d -s service 'uv run python -m panopticon.taskservice'
	tmux -L panopticon has-session -t runner 2>/dev/null || \
		tmux -L panopticon new-session -d -s runner 'uv run python -m panopticon.sessionservice.host'
	uv run panopticon console

stop:  ## Stop everything `make start` started: the task containers + the -L panopticon tmux server
	-docker ps --all --quiet --filter name=^panopticon- | xargs --no-run-if-empty docker rm --force
	-tmux -L panopticon kill-server 2>/dev/null

build:  ## Build the base task-container image (override with IMAGE=)
	docker build --tag $(IMAGE) --file docker/Dockerfile .

clean:  ## Remove the base image and any composed panopticon-* images
	-docker rmi --force $(IMAGE)
	-docker images --quiet 'panopticon-*' | sort --unique | xargs --no-run-if-empty docker rmi --force
