# panopticon — dev tasks. Thin wrappers over `uv`/`docker`; see CLAUDE.md for details.
.DEFAULT_GOAL := help
.PHONY: help sync test typecheck check serve dashboard panopticon login build clean

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

serve:  ## Run the task service over HTTP (the control plane)
	uv run python -m panopticon.taskservice

dashboard:  ## Launch the dashboard (foreground; no tmux)
	uv run panopticon dashboard

panopticon:  ## Run the session supervisor (dashboard + task attach); task service in the background
	tmux -L panopticon has-session -t service 2>/dev/null || \
		tmux -L panopticon new-session -d -s service 'uv run python -m panopticon.taskservice'
	uv run panopticon console

login:  ## Populate a repo's creds volume interactively (REPO=<id>)
	uv run panopticon login $(REPO)

build:  ## Build the base task-container image (override with IMAGE=)
	docker build --tag $(IMAGE) --file docker/Dockerfile .

clean:  ## Remove the base image and any composed panopticon-* images
	-docker rmi --force $(IMAGE)
	-docker images --quiet 'panopticon-*' | sort --unique | xargs --no-run-if-empty docker rmi --force
