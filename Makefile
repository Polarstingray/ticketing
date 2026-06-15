# Stingray Tickets — common tasks. `make help` lists them.
DC ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help install up down logs restart test lint backend-test resolver-test frontend-test

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

test: backend-test resolver-test frontend-test ## Run all test suites

backend-test: ## Run backend pytest suite
	cd backend && python -m pytest -q

resolver-test: ## Run resolver pytest suite
	cd resolver && python -m pytest -q

frontend-test: ## Run frontend tests
	cd frontend && npm test --silent

lint: ## Ruff-lint the Python code
	ruff check backend resolver
