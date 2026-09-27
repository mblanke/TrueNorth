# ------------------------------------------------------------------
# TrueNorth Range — Makefile
# ------------------------------------------------------------------
.PHONY: help dev dev-down import-content test lint format build clean migrate \
                packer-validate tf-plan k6 pre-commit security-scan \
                prod-config prod-up prod-down prod-ps prod-logs

SHELL := /bin/bash
COMPOSE := docker compose -f infra/platform/docker/compose.dev.yml
# The production stack keeps its env file outside the checkout so secrets are
# never written to a tracked path. Override ENV_FILE_PROD if yours lives elsewhere.
ENV_FILE_PROD ?= /srv/truenorth/config/.env.production
COMPOSE_PROD := docker compose -f infra/platform/docker/compose.prod.yml --env-file $(ENV_FILE_PROD)
PYTHON  := python3

# Colours
_CYAN  := \033[36m
_RESET := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "$(_CYAN)%-20s$(_RESET) %s\n", $$1, $$2}'

# ── Development ─────────────────────────────────────────────
dev: ## Start dev stack (docker compose up --build)
	$(COMPOSE) up -d --build --remove-orphans
	@echo "✓ Dev stack running"
	@echo "  Web + /api/: http://localhost:4200   (nginx; this is the front door)"
	@echo "  API direct:  http://127.0.0.1:8081   (loopback only, by design)"
	@echo "  AI:          http://localhost:6000"
	@echo "  Keycloak:    http://localhost:8180"
	@echo "  Flower:      http://localhost:5555"

dev-down: ## Stop dev stack
	$(COMPOSE) down --remove-orphans

dev-logs: ## Tail logs for all services
	$(COMPOSE) logs -f --tail=100

# A fresh dev database is empty: `make dev` loads no content. This loads everything
# under content/ through the API (curriculum, VM image catalogue, range templates,
# scenarios, detection rules, plus demo people, ranges and exercises) and is safe to
# re-run. Stdlib-only Python, so a stock macOS python3 is enough. For a deployment
# with auth on: python3 scripts/load_content.py --api URL --token TOKEN
DEV_API ?= http://127.0.0.1:8081

import-content: ## Load all content (curriculum, templates, scenarios, detections, demo data)
	python3 scripts/load_content.py --api $(DEV_API)

# ── Production ──────────────────────────────────────────────
# Normally driven by install/ (Ansible) on the platform host; these targets are
# for local inspection and break-glass operation.
prod-config: ## Validate the production compose file resolves
	$(COMPOSE_PROD) config >/dev/null && echo "✓ compose.prod.yml resolves"

prod-up: ## Start the production stack
	$(COMPOSE_PROD) up -d --build --remove-orphans
	@echo "✓ Production stack starting — check 'make prod-ps' until all are healthy"

prod-down: ## Stop the production stack
	$(COMPOSE_PROD) down --remove-orphans

prod-ps: ## Show production service health
	$(COMPOSE_PROD) ps

prod-logs: ## Tail production logs
	$(COMPOSE_PROD) logs -f --tail=100

# ── Testing ─────────────────────────────────────────────────
test: ## Run full pytest suite with coverage
	$(PYTHON) -m pytest \
	--cov=control-plane/api \
	--cov=control-plane/worker \
	--cov=scenario-engine \
	--cov=ai-orchestrator \
	--cov-report=term-missing \
	--cov-report=html:htmlcov \
	-v --tb=short

test-fast: ## Run tests excluding slow/integration
	$(PYTHON) -m pytest -m "not slow and not integration" -v --tb=short

test-api: ## Run API tests only
	$(PYTHON) -m pytest tests/api/ -v --tb=short

test-scenario: ## Run scenario-engine tests only
	$(PYTHON) -m pytest tests/scenario_engine/ -v --tb=short

test-worker: ## Run worker tests only
	$(PYTHON) -m pytest tests/worker/ -v --tb=short

# ── Linting & Formatting ───────────────────────────────────
lint: ## Ruff check + fix
	ruff check . --fix
	ruff format --check .

format: ## Ruff format (apply)
	ruff format .
	ruff check . --fix

typecheck: ## Run mypy
	mypy control-plane/api/app control-plane/worker/worker \
	ai-orchestrator/app scenario-engine/scenario_engine \
	--ignore-missing-imports

# ── Docker Builds ──────────────────────────────────────────
build: ## Build all docker images
	docker build -t truenorth-api:latest       control-plane/api/
	docker build -t truenorth-worker:latest    control-plane/worker/
	docker build -t truenorth-ai:latest        ai-orchestrator/
	docker build -t truenorth-web:latest       control-plane/web/
	@echo "✓ All images built"

build-api: ## Build API image only
	docker build -t truenorth-api:latest control-plane/api/

build-worker: ## Build worker image only
	docker build -t truenorth-worker:latest control-plane/worker/

build-web: ## Build web image only
	docker build -t truenorth-web:latest control-plane/web/

build-ai: ## Build AI orchestrator image only
	docker build -t truenorth-ai:latest ai-orchestrator/

# ── Database ───────────────────────────────────────────────
migrate: ## Run alembic upgrade head
	alembic upgrade head

migrate-gen: ## Generate alembic migration (usage: make migrate-gen MSG="add users table")
	alembic revision --autogenerate -m "$(MSG)"

migrate-down: ## Downgrade one alembic revision
	alembic downgrade -1

# ── Infrastructure ─────────────────────────────────────────
packer-validate: ## Validate all Packer templates
	@cd infra/proxmox/packer && \
	for hcl in *.pkr.hcl; do \
	echo "Validating $$hcl..."; \
	packer validate -syntax-only "$$hcl" && echo "  ✓ $$hcl" || echo "  ✗ $$hcl"; \
	done

tf-plan: ## Terraform plan (infra/proxmox/terraform)
	cd infra/proxmox/terraform && terraform init -input=false && terraform plan

tf-validate: ## Terraform validate
	cd infra/proxmox/terraform && terraform init -input=false && terraform validate

# ── Load Testing ───────────────────────────────────────────
k6: ## Run k6 load tests
	k6 run tests/load/k6-script.js

k6-smoke: ## Run k6 smoke test (low load)
	k6 run --vus 5 --duration 30s tests/load/k6-script.js

# ── Cleanup ────────────────────────────────────────────────
clean: ## Remove __pycache__, .pytest_cache, build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name coverage.xml -delete 2>/dev/null || true
	find . -type f -name .coverage -delete 2>/dev/null || true
	rm -rf dist build .eggs
	@echo "✓ Cleaned"

clean-docker: ## Remove all truenorth docker images and volumes
	docker images --filter "reference=truenorth-*" -q | xargs -r docker rmi -f
	$(COMPOSE) down -v --remove-orphans
	@echo "✓ Docker cleaned"

# ── Pre-commit ─────────────────────────────────────────────
pre-commit: ## Run pre-commit on all files
	pre-commit run --all-files

pre-commit-install: ## Install pre-commit hooks
	pip install pre-commit
	pre-commit install
	@echo "✓ Pre-commit hooks installed"

# ── Security ───────────────────────────────────────────────
security-scan: ## Run bandit + safety checks
	bandit -r control-plane/api/app control-plane/worker/worker \
	ai-orchestrator/app scenario-engine/scenario_engine \
	--severity-level medium
	pip-audit || echo "pip-audit not installed — skipping"