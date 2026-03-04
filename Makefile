# ------------------------------------------------------------------
# TrueNorth Range — Makefile
# ------------------------------------------------------------------
.PHONY: help dev dev-down test lint format build clean migrate \
        packer-validate tf-plan k6 pre-commit security-scan

SHELL := /bin/bash
COMPOSE := docker compose -f infra/platform/docker/compose.dev.yml
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
@echo "  API:    http://localhost:8080"
@echo "  Web:    http://localhost:4200"
@echo "  AI:     http://localhost:6000"
@echo "  PgAdmin: http://localhost:5050"

dev-down: ## Stop dev stack
$(COMPOSE) down --remove-orphans

dev-logs: ## Tail logs for all services
$(COMPOSE) logs -f --tail=100

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