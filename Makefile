# Stingray Tickets — common tasks. `make help` lists them.
DC ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help install up down logs restart test lint backend-test resolver-test cli-test frontend-test desktop-dev desktop-build

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

test: backend-test resolver-test cli-test frontend-test ## Run all test suites

backend-test: ## Run backend pytest suite
	cd backend && python -m pytest -q

resolver-test: ## Run resolver pytest suite
	cd resolver && python -m pytest -q

cli-test: ## Run stingray CLI pytest suite
	cd cli && python -m pytest -q

frontend-test: ## Run frontend tests
	cd frontend && npm test --silent

lint: ## Ruff-lint the Python code
	ruff check backend resolver cli

desktop-dev: ## Run the desktop app in dev mode (needs Rust + Node)
	cd desktop && npm install && npm run tauri:dev

desktop-build: ## Build the desktop installers (.deb/.AppImage/.dmg)
	# fakeroot makes the .deb bundler record root-owned (uid/gid 0) files; without
	# it a large login UID overflows Debian's ar/tar header fields and corrupts the
	# .deb. Falls back to a plain build where fakeroot isn't installed (e.g. macOS).
	# APPIMAGE_EXTRACT_AND_RUN lets linuxdeploy run without FUSE, which many VMs lack.
	cd desktop && npm ci && \
		APPIMAGE_EXTRACT_AND_RUN=1 $$(command -v fakeroot) npm run tauri:build
