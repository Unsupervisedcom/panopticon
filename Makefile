# panopticon — dev tasks. Thin wrappers over `uv`/`docker`; see CLAUDE.md for details.
.DEFAULT_GOAL := help
.PHONY: help sync test typecheck check serve dashboard panopticon build clean

#: The base task-container image (ADR 0005 base layer); must match DEFAULT_IMAGE.
IMAGE ?= panopticon-base

help:  ## List available targets
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | sort | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## Create the venv and install dependencies
	uv sync

test:  ## Run the test suite
	uv run pytest

typecheck:  ## Type-check (mypy, strict)
	uv run mypy -p panopticon

check: typecheck test  ## Type-check + tests (what CI runs)

serve:  ## Run the task service over HTTP (the control plane)
	uv run python -m panopticon.taskservice

dashboard:  ## Launch the dashboard (foreground; no tmux)
	uv run panopticon dashboard

panopticon:  ## Start the task service + dashboard in one panopticon tmux session (the switcher)
	tmux -L panopticon has-session -t panopticon 2>/dev/null || { \
		tmux -L panopticon new-session -d -s panopticon -n service 'uv run python -m panopticon.taskservice'; \
		tmux -L panopticon new-window -t panopticon -n dashboard 'uv run panopticon dashboard'; \
	}
	tmux -L panopticon attach -t panopticon

build:  ## Build the base task-container image (override with IMAGE=)
	docker build -t $(IMAGE) -f docker/Dockerfile .

clean:  ## Remove the base image and any composed panopticon-* images
	-docker rmi -f $(IMAGE)
	-docker images -q 'panopticon-*' | sort -u | xargs -r docker rmi -f
