# TrueNorth Range — Build and Release Guide

> CI/CD pipeline architecture, build matrix, Docker and Packer image builds, Terraform workflows, release process, and version numbering.

---

## Table of Contents

- [CI/CD Pipeline Overview](#cicd-pipeline-overview)
- [Pipeline Architecture](#pipeline-architecture)
- [Build Matrix](#build-matrix)
- [Docker Image Builds](#docker-image-builds)
- [Packer Image Builds](#packer-image-builds)
- [Terraform Workflows](#terraform-workflows)
- [Testing Pipeline](#testing-pipeline)
- [Release Process](#release-process)
- [Version Numbering](#version-numbering)
- [Artifact Registry](#artifact-registry)
- [Environment Promotion](#environment-promotion)
- [Rollback Procedures](#rollback-procedures)

---

## CI/CD Pipeline Overview

TrueNorth Range uses GitHub Actions for CI/CD with the following workflow triggers:

| Trigger | Pipeline | Actions |
|---------|----------|---------|
| Push to `main` | Full CI | Lint, test, build, push images, deploy to staging |
| Push to `develop` | CI | Lint, test, build (no push) |
| Pull Request | PR Check | Lint, test, security scan |
| Tag `v*` | Release | Full CI + tag images + deploy to production |
| Manual dispatch | Custom | Selectable: build, deploy, packer, terraform |
| Schedule (weekly) | Security | Dependency scan, container scan |

```mermaid
flowchart LR
    subgraph Triggers
        PR[Pull Request]
        PUSH[Push to main]
        TAG[Tag v*]
        SCHED[Schedule]
    end

    subgraph CI Pipeline
        LINT[Lint & Format]
        TEST[Unit Tests]
        ITEST[Integration Tests]
        SEC[Security Scan]
        BUILD[Docker Build]
    end

    subgraph CD Pipeline
        PUSH_IMG[Push Images]
        STAGE[Deploy Staging]
        SMOKE[Smoke Tests]
        PROD[Deploy Production]
    end

    PR --> LINT --> TEST --> SEC
    PUSH --> LINT --> TEST --> ITEST --> BUILD --> PUSH_IMG --> STAGE --> SMOKE
    TAG --> LINT --> TEST --> ITEST --> BUILD --> PUSH_IMG --> STAGE --> SMOKE --> PROD
    SCHED --> SEC
```

---

## Pipeline Architecture

### GitHub Actions Workflow Files

```
.github/workflows/
+-- ci.yml              # Main CI pipeline (push/PR)
+-- release.yml         # Release pipeline (tag)
+-- security.yml        # Weekly security scan
+-- packer.yml          # Packer image build (manual)
+-- terraform.yml       # Terraform plan/apply (manual)
+-- docs.yml            # Documentation build and deploy
```

### ci.yml — Main CI Pipeline

```yaml
name: CI
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_PREFIX: ghcr.io/${{ github.repository }}

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install tools
        run: pip install ruff mypy
      - name: Ruff lint
        run: ruff check control-plane/ ai-orchestrator/ scenario-engine/
      - name: Ruff format check
        run: ruff format --check control-plane/ ai-orchestrator/ scenario-engine/
      - name: MyPy type check
        run: mypy control-plane/api/app/ --ignore-missing-imports

  test-unit:
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          cd control-plane/api
          pip install -r requirements.txt -r requirements-dev.txt
      - name: Run unit tests
        run: pytest tests/unit/ -v --cov=app --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          file: coverage.xml

  test-integration:
    runs-on: ubuntu-latest
    needs: test-unit
    if: github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/')
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: truenorth_test
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
      redis:
        image: redis:7
        ports: ["6379:6379"]
      opensearch:
        image: opensearchproject/opensearch:2.13.0
        env:
          discovery.type: single-node
          DISABLE_SECURITY_PLUGIN: "true"
        ports: ["9200:9200"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          cd control-plane/api
          pip install -r requirements.txt -r requirements-dev.txt
      - name: Run integration tests
        env:
          DATABASE_URL: postgresql://test:test@localhost:5432/truenorth_test
          REDIS_URL: redis://localhost:6379/0
          OPENSEARCH_URL: http://localhost:9200
        run: pytest tests/integration/ -v --timeout=120

  security-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Pip audit
        run: pip-audit --requirement control-plane/api/requirements.txt
      - name: Trivy filesystem scan
        uses: aquasecurity/trivy-action@master
        with:
          scan-type: fs
          scan-ref: .
          severity: HIGH,CRITICAL

  build:
    runs-on: ubuntu-latest
    needs: [test-unit, security-scan]
    strategy:
      matrix:
        component:
          - { name: control-plane-api, context: control-plane/api }
          - { name: celery-worker, context: control-plane/worker }
          - { name: ai-orchestrator, context: ai-orchestrator }
          - { name: scenario-engine, context: scenario-engine }
          - { name: frontend, context: frontend }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: ${{ matrix.component.context }}
          push: ${{ github.ref == 'refs/heads/main' }}
          tags: |
            ${{ env.IMAGE_PREFIX }}/${{ matrix.component.name }}:${{ github.sha }}
            ${{ env.IMAGE_PREFIX }}/${{ matrix.component.name }}:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy-staging:
    runs-on: ubuntu-latest
    needs: build
    if: github.ref == 'refs/heads/main'
    environment: staging
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to staging
        run: |
          helm upgrade --install truenorth charts/truenorth \
            --namespace staging \
            --set image.tag=${{ github.sha }} \
            -f charts/truenorth/values-staging.yaml
      - name: Run smoke tests
        run: |
          sleep 30
          pytest tests/smoke/ -v --base-url=https://staging.truenorth.local
```

---

## Build Matrix

### Component Build Matrix

| Component | Language | Build Tool | Base Image | Output |
|-----------|---------|-----------|-----------|--------|
| Control Plane API | Python 3.12 | pip + Docker | `python:3.12-slim` | Container |
| Celery Worker | Python 3.12 | pip + Docker | `python:3.12-slim` | Container |
| AI Orchestrator | Python 3.12 | pip + Docker | `python:3.12-slim` | Container |
| Scenario Engine | Python 3.12 | pip + Docker | `python:3.12-slim` | Container |
| Frontend | TypeScript | npm + Docker | `node:20-alpine` → `nginx:alpine` | Container |
| Range Templates | HCL + Shell | Packer | Various OS ISOs | VM Images |
| Infrastructure | HCL | Terraform | N/A | Cloud Resources |

### Python Dependency Matrix

| Component | Key Dependencies | Version Constraints |
|-----------|-----------------|-------------------|
| Control Plane API | FastAPI 0.115+, SQLAlchemy 2.0+, Pydantic 2.0+, uvicorn | Pinned in requirements.txt |
| Celery Worker | Celery 5.3+, redis 5.0+, python-terraform | Pinned in requirements.txt |
| AI Orchestrator | FastAPI 0.115+, openai 1.0+, anthropic 0.20+, httpx | Pinned in requirements.txt |
| Scenario Engine | pyyaml 6.0+, opensearch-py 2.4+, minio 7.2+ | Pinned in requirements.txt |

---

## Docker Image Builds

### Dockerfile Pattern

All components follow a multi-stage build pattern:

```dockerfile
# Stage 1: Build
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim AS runtime

# Security: non-root user
RUN groupadd -r truenorth && useradd -r -g truenorth truenorth

WORKDIR /app
COPY --from=builder /install /usr/local
COPY app/ ./app/

# Security: no shell, read-only where possible
USER truenorth
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Frontend Dockerfile

```dockerfile
# Stage 1: Build Angular
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build -- --configuration=production

# Stage 2: Serve with Nginx
FROM nginx:1.25-alpine
COPY --from=builder /app/dist/truenorth-frontend/browser/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

### Local Build Commands

```bash
# Build all images
docker compose -f infra/platform/docker/compose.dev.yml build

# Build specific component
docker build -t truenorth/control-plane-api:dev control-plane/api/

# Build with BuildKit cache
DOCKER_BUILDKIT=1 docker build \
  --cache-from truenorth/control-plane-api:latest \
  -t truenorth/control-plane-api:dev \
  control-plane/api/

# Scan built image
trivy image truenorth/control-plane-api:dev
```

### Image Size Targets

| Image | Target Size | Actual |
|-------|-----------|--------|
| control-plane-api | <200 MB | ~180 MB |
| celery-worker | <200 MB | ~185 MB |
| ai-orchestrator | <250 MB | ~220 MB |
| scenario-engine | <150 MB | ~140 MB |
| frontend | <50 MB | ~35 MB |

---

## Packer Image Builds

### Overview

Packer builds VM templates for range environments. Each template produces a Proxmox VM template that Terraform uses for provisioning.

### Packer Template Structure

```
infra/packer/
+-- windows-server-2022/
|   +-- windows-server-2022.pkr.hcl
|   +-- scripts/
|   |   +-- install-sysmon.ps1
|   |   +-- configure-winrm.ps1
|   |   +-- install-ad.ps1
|   |   +-- harden.ps1
|   +-- answer-file/
|       +-- autounattend.xml
+-- windows-11/
|   +-- windows-11.pkr.hcl
|   +-- scripts/
|       +-- install-sysmon.ps1
|       +-- install-office.ps1
+-- ubuntu-2204/
|   +-- ubuntu-2204.pkr.hcl
|   +-- scripts/
|       +-- install-zeek.sh
|       +-- install-auditd.sh
|       +-- install-elk.sh
|       +-- harden.sh
+-- kali-2024/
|   +-- kali-2024.pkr.hcl
|   +-- scripts/
|       +-- install-tools.sh
+-- pfsense-27/
    +-- pfsense-27.pkr.hcl
    +-- config/
        +-- config.xml
```

### Building Packer Images

```bash
# Validate template
cd infra/packer/windows-server-2022
packer validate -var-file=../variables.pkrvars.hcl .

# Build template
packer build -var-file=../variables.pkrvars.hcl .

# Build with specific Proxmox target
packer build \
  -var "proxmox_url=https://pve.truenorth.local:8006/api2/json" \
  -var "proxmox_node=pve01" \
  -var-file=../variables.pkrvars.hcl \
  .

# Build all templates (CI/CD)
for dir in infra/packer/*/; do
  packer build -var-file=infra/packer/variables.pkrvars.hcl "$dir"
done
```

### Packer Build Pipeline (GitHub Actions)

```yaml
name: Packer Build
on:
  workflow_dispatch:
    inputs:
      template:
        description: Template to build
        type: choice
        options:
          - windows-server-2022
          - windows-11
          - ubuntu-2204
          - kali-2024
          - pfsense-27
          - all

jobs:
  build:
    runs-on: [self-hosted, proxmox]
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-packer@main
      - name: Packer init
        run: packer init infra/packer/${{ inputs.template }}/
      - name: Packer build
        env:
          PROXMOX_URL: ${{ secrets.PROXMOX_URL }}
          PROXMOX_TOKEN: ${{ secrets.PROXMOX_TOKEN }}
        run: |
          packer build \
            -var "proxmox_url=$PROXMOX_URL" \
            -var "proxmox_token=$PROXMOX_TOKEN" \
            -var-file=infra/packer/variables.pkrvars.hcl \
            infra/packer/${{ inputs.template }}/
```

---

## Terraform Workflows

### Terraform Module Structure

```
infra/terraform/
+-- modules/
|   +-- range/
|   |   +-- main.tf          # VM provisioning
|   |   +-- variables.tf
|   |   +-- outputs.tf
|   |   +-- network.tf       # VLAN creation
|   +-- platform/
|       +-- main.tf           # Platform infrastructure
|       +-- database.tf
|       +-- redis.tf
|       +-- opensearch.tf
+-- environments/
    +-- dev/
    |   +-- main.tf
    |   +-- terraform.tfvars
    +-- staging/
    |   +-- main.tf
    |   +-- terraform.tfvars
    +-- production/
        +-- main.tf
        +-- terraform.tfvars
        +-- backend.tf        # S3 state backend
```

### Terraform Commands

```bash
# Plan (review changes)
cd infra/terraform/environments/production
terraform init
terraform plan -out=tfplan

# Apply (execute changes)
terraform apply tfplan

# Range provisioning (called by Celery worker)
cd infra/terraform/modules/range
terraform init
terraform plan \
  -var="range_id=<uuid>" \
  -var="template=medium-enterprise" \
  -var="vlan_id=101" \
  -out=tfplan
terraform apply tfplan

# Destroy a range
terraform destroy \
  -var="range_id=<uuid>" \
  -var="template=medium-enterprise" \
  -var="vlan_id=101"
```

### State Management

| Environment | Backend | Lock |
|------------|---------|------|
| Development | Local | N/A |
| Staging | S3 (MinIO) | DynamoDB (or PostgreSQL) |
| Production | S3 (MinIO) + encryption | DynamoDB (or PostgreSQL) |

```hcl
# Production backend configuration
terraform {
  backend "s3" {
    bucket         = "truenorth-terraform-state"
    key            = "production/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "truenorth-terraform-locks"
    endpoint       = "https://minio.truenorth.local"
  }
}
```

---

## Testing Pipeline

### Test Hierarchy

```
tests/
+-- unit/                     # Fast, isolated, no external deps
|   +-- test_models.py
|   +-- test_rbac.py
|   +-- test_state_machines.py
+-- integration/              # With real databases/services
|   +-- test_ranges_api.py
|   +-- test_exercises_api.py
|   +-- test_websocket.py
+-- smoke/                    # Post-deployment validation
|   +-- test_health.py
|   +-- test_basic_workflow.py
+-- e2e/                      # Full user journeys
|   +-- test_provision_exercise_aar.py
+-- scenario_engine/
    +-- test_content_validation.py
    +-- test_injectors.py
    +-- test_validators.py
```

### Test Execution

```bash
# Unit tests (fast — ~30s)
pytest tests/unit/ -v --cov

# Integration tests (requires services — ~2min)
pytest tests/integration/ -v --timeout=120

# Smoke tests (against running instance — ~30s)
pytest tests/smoke/ -v --base-url=https://staging.truenorth.local

# All tests
pytest tests/ -v --cov --timeout=300

# Specific test file
pytest tests/unit/test_rbac.py -v

# With coverage report
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html
```

### Coverage Targets

| Component | Target | Current |
|-----------|--------|---------|
| Models | >90% | 92% |
| RBAC | >95% | 96% |
| Routers | >80% | 83% |
| WebSocket Manager | >75% | 78% |
| Overall | >80% | 85% |

---

## Release Process

### Release Workflow

```mermaid
flowchart LR
    A[Feature Branch] --> B[PR to develop]
    B --> C[Code Review]
    C --> D[Merge to develop]
    D --> E[PR to main]
    E --> F[Integration Tests]
    F --> G[Merge to main]
    G --> H[Create Tag v*]
    H --> I[Release Pipeline]
    I --> J[Deploy Staging]
    J --> K[Smoke Tests]
    K --> L[Deploy Production]
    L --> M[Post-deploy Validation]
```

### Step-by-Step Release

1. **Prepare release branch:**
   ```bash
   git checkout main
   git pull origin main
   ```

2. **Update version numbers:**
   ```bash
   # Update pyproject.toml / setup.py
   # Update package.json (frontend)
   # Update Chart.yaml (Helm)
   ```

3. **Update CHANGELOG.md:**
   ```markdown
   ## [1.5.0] - 2026-01-15
   ### Added
   - New "supply-chain" scenario
   - Batch provision endpoint
   ### Fixed
   - WebSocket reconnection on token refresh
   ### Changed
   - Upgraded OpenSearch client to 2.5
   ```

4. **Create and push tag:**
   ```bash
   git tag -a v1.5.0 -m "Release 1.5.0 - Supply chain scenario"
   git push origin v1.5.0
   ```

5. **Monitor release pipeline:**
   - CI runs full test suite
   - Docker images built and tagged with `v1.5.0`
   - Helm chart published
   - Staging deployment triggered
   - Smoke tests run against staging

6. **Approve production deployment:**
   - Manual approval gate in GitHub Actions
   - Production deployment via Helm upgrade
   - Post-deployment smoke tests

7. **Create GitHub Release:**
   - Auto-generated release notes
   - Link to CHANGELOG
   - Attach any binary artifacts

---

## Version Numbering

### Semantic Versioning

TrueNorth Range follows [Semantic Versioning 2.0.0](https://semver.org/):

```
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]

Examples:
  1.5.0           # Stable release
  1.5.1           # Patch fix
  2.0.0-rc.1      # Release candidate
  2.0.0-beta.3    # Beta release
  1.5.0+20260115  # Build metadata
```

| Change | Version Impact | Examples |
|--------|---------------|---------|
| Breaking API changes | MAJOR | Remove endpoint, change response schema |
| Breaking DB migrations | MAJOR | Non-reversible schema change |
| New features | MINOR | New endpoint, new injector type |
| Bug fixes | PATCH | Fix crash, correct calculation |
| Security patches | PATCH | Dependency update for CVE |

### Version Locations

| File | Contains Version |
|------|-----------------|
| `control-plane/api/app/__init__.py` | `__version__ = "1.5.0"` |
| `control-plane/api/pyproject.toml` | `version = "1.5.0"` |
| `frontend/package.json` | `"version": "1.5.0"` |
| `charts/truenorth/Chart.yaml` | `version: 1.5.0`, `appVersion: 1.5.0` |
| `CHANGELOG.md` | Release history |

---

## Artifact Registry

### Container Images

| Image | Registry | Tags |
|-------|---------|------|
| `truenorth/control-plane-api` | ghcr.io | `latest`, `v1.5.0`, `sha-abc1234` |
| `truenorth/celery-worker` | ghcr.io | `latest`, `v1.5.0`, `sha-abc1234` |
| `truenorth/ai-orchestrator` | ghcr.io | `latest`, `v1.5.0`, `sha-abc1234` |
| `truenorth/scenario-engine` | ghcr.io | `latest`, `v1.5.0`, `sha-abc1234` |
| `truenorth/frontend` | ghcr.io | `latest`, `v1.5.0`, `sha-abc1234` |

### Helm Chart

```bash
# Add chart repository
helm repo add truenorth https://charts.truenorth.local

# Install specific version
helm install truenorth truenorth/truenorth --version 1.5.0
```

### Packer Images

Stored in Proxmox template storage with naming convention:
```
truenorth-{os}-{version}-{build-date}
# Examples:
truenorth-win2022-1.5.0-20260115
truenorth-ubuntu2204-1.5.0-20260115
truenorth-kali2024-1.5.0-20260115
```

---

## Environment Promotion

```
develop → staging → production
```

| Environment | Deploy Trigger | Approval | URL |
|------------|---------------|----------|-----|
| Development | Push to `develop` | Automatic | `https://dev.truenorth.local` |
| Staging | Push to `main` | Automatic | `https://staging.truenorth.local` |
| Production | Tag `v*` | Manual approval | `https://truenorth.local` |

### Promotion Checklist

Before promoting staging → production:

- [ ] All smoke tests passing on staging
- [ ] No new errors in staging logs (24h observation)
- [ ] Performance metrics within acceptable range
- [ ] Database migration tested and reversible
- [ ] Rollback plan documented and tested
- [ ] Release notes prepared
- [ ] Team notified of deployment window

---

## Rollback Procedures

### Application Rollback

```bash
# Rollback to previous revision
kubectl rollout undo deployment truenorth-api -n production
kubectl rollout undo deployment celery-worker -n production

# Rollback to specific version
kubectl set image deployment/truenorth-api \
  api=ghcr.io/truenorth/control-plane-api:v1.4.0 \
  -n production

# Via Helm
helm rollback truenorth 1 -n production  # Rollback to revision 1
```

### Database Rollback

```bash
# Rollback last migration
cd control-plane/api
alembic downgrade -1

# Rollback to specific revision
alembic downgrade abc123def456

# Check current revision
alembic current

# View migration history
alembic history --verbose
```

### Full Rollback Procedure

1. **Assess impact** — Determine if rollback is necessary
2. **Notify team** — Alert all stakeholders
3. **Rollback application** — Helm rollback or kubectl undo
4. **Rollback database** — Alembic downgrade (if migration was applied)
5. **Verify health** — Check all health endpoints
6. **Run smoke tests** — Validate core workflows
7. **Monitor** — Watch error rates for 30 minutes
8. **Post-mortem** — Document what went wrong