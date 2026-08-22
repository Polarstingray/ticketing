# Stingray Tickets — common tasks. `make help` lists them.
DC ?= docker compose

# Resolve a Python interpreter at recipe time, into $$p.
#
# A bare `python` is absent on plenty of systems that have `python3` (Debian and
# Ubuntu among them), so hardcoding it makes these targets fail as "command not
# found" rather than as a test result. The system interpreter is not enough on its
# own either — pytest and the app's dependencies live in a virtualenv, not on it.
#
# Order: the project's own .venv, then the backend's (the fullest one in the repo,
# and what cli/ uses since it ships no venv of its own), then whatever is on PATH.
# Override for a one-off with `make backend-test PY=/path/to/python`.
PY ?=
define resolve_py
p="$(PY)"; \
if [ -z "$$p" ]; then \
  for c in .venv/bin/python ../backend/.venv/bin/python; do \
    [ -x "$$c" ] && { p="$$c"; break; }; \
  done; \
fi; \
[ -n "$$p" ] || p="$$(command -v python3 || command -v python)"; \
if [ -z "$$p" ]; then echo "no python interpreter found" >&2; exit 1; fi; \
"$$p" -c "import pytest" 2>/dev/null || { \
  echo "$$p has no pytest — create the venv (see README > Local development)" >&2; \
  exit 1; \
}
endef

.DEFAULT_GOAL := help

.PHONY: help install up down logs restart test lint backend-test resolver-test cli-test frontend-test desktop-dev desktop-build deploy deploy-log hooks-install hooks-uninstall

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Run the guided installer (generates secrets, brings everything up)
	./install.sh

up: ## Start all services in the background
	$(DC) up -d --build

down: ## Stop all services
	$(DC) down

restart: ## Restart all services
	$(DC) restart

logs: ## Tail backend logs
	$(DC) logs -f backend

deploy: ## Rebuild + restart locally, gated on the test suites
	./deploy/autodeploy.sh manual

deploy-log: ## Tail the auto-deploy log
	tail -f deploy/.autodeploy.log

venv-sync: ## Reinstall any venv whose requirements.txt changed (runs on pull too)
	./deploy/autodeploy.sh venv-sync-only

hooks-install: ## Auto-deploy on every commit/merge to main
	./deploy/install-hooks.sh

hooks-uninstall: ## Remove the auto-deploy git hooks
	./deploy/install-hooks.sh --uninstall

test: backend-test resolver-test cli-test frontend-test ## Run all test suites

backend-test: ## Run backend pytest suite
	@cd backend && $(resolve_py) && "$$p" -m pytest -q

resolver-test: ## Run resolver pytest suite
	@cd resolver && $(resolve_py) && "$$p" -m pytest -q

cli-test: ## Run stingray CLI pytest suite
	@cd cli && $(resolve_py) && "$$p" -m pytest -q

frontend-test: ## Run frontend tests
	cd frontend && npm test --silent

lint: ## Ruff-lint the Python code
	@r="$$(command -v ruff || echo backend/.venv/bin/ruff)"; \
	 [ -x "$$r" ] || { echo "ruff not found; pip install ruff" >&2; exit 1; }; \
	 "$$r" check backend resolver cli

desktop-dev: ## Run the desktop app in dev mode (needs Rust + Node)
	cd desktop && npm install && npm run tauri:dev

desktop-build: ## Build the desktop installers (.deb/.AppImage/.dmg)
	# fakeroot makes the .deb bundler record root-owned (uid/gid 0) files; without
	# it a large login UID overflows Debian's ar/tar header fields and corrupts the
	# .deb. Falls back to a plain build where fakeroot isn't installed (e.g. macOS).
	# APPIMAGE_EXTRACT_AND_RUN lets linuxdeploy run without FUSE, which many VMs lack.
	cd desktop && npm ci && \
		APPIMAGE_EXTRACT_AND_RUN=1 $$(command -v fakeroot) npm run tauri:build
