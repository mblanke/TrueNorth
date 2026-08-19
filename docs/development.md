# TrueNorth Range — Development Guide

> Complete developer guide covering local setup, testing, code style, database migrations, and extending the platform.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Repository Structure](#repository-structure)
- [Local Setup](#local-setup)
- [Running Tests](#running-tests)
- [Code Style and Linting](#code-style-and-linting)
- [Database Migrations](#database-migrations)
- [Seed Data and Content Import](#seed-data-and-content-import)
- [Adding New API Endpoints](#adding-new-api-endpoints)
- [Adding New Scenario Injectors](#adding-new-scenario-injectors)
- [Adding New Packer Templates](#adding-new-packer-templates)
- [Frontend Development](#frontend-development)
- [Docker Development Workflow](#docker-development-workflow)
- [Debugging Tips](#debugging-tips)
- [Development Workflow](#development-workflow)
- [CLI Tool Reference](#cli-tool-reference)
- [SDK Usage](#sdk-usage)

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | API, worker, scenario engine, CLI |
| Node.js | 20+ | Angular frontend |
| Docker | 24+ | Container runtime |
| Docker Compose | v2.24+ | Service orchestration |
| Git | 2.40+ | Version control |
| ruff | Latest | Python linting and formatting |
| mypy | Latest | Static type checking |
| pre-commit | Latest | Git hook management |

### Install Prerequisites (Windows)

```powershell
# Python (via winget)
winget install Python.Python.3.11

# Node.js
winget install OpenJS.NodeJS.LTS

# Docker Desktop
winget install Docker.DockerDesktop

# Development tools
pip install ruff mypy pre-commit
```

### Install Prerequisites (macOS)

```bash
brew install python@3.11 node@20 docker docker-compose
pip install ruff mypy pre-commit
```

### Install Prerequisites (Linux)

```bash
sudo apt install python3.11 python3.11-venv nodejs npm docker.io docker-compose-v2
pip install ruff mypy pre-commit
```

---

## Repository Structure

```
Core_Docs/
+-- control-plane/
|   +-- api/                        # FastAPI backend (main application)
|   |   +-- app/
|   |   |   +-- __init__.py
|   |   |   +-- main.py             # App factory, middleware, lifespan events
|   |   |   +-- models.py           # SQLAlchemy ORM (12 models, 282 lines)
|   |   |   +-- schemas.py          # Pydantic v2 schemas (request/response)
|   |   |   +-- auth.py             # Keycloak JWT validation middleware
|   |   |   +-- rbac.py             # Fine-grained RBAC (25 perms, 5 roles, 280 lines)
|   |   |   +-- db.py               # Database engine, session factory
|   |   |   +-- websocket_manager.py # Production WS (Redis pub/sub, 476 lines)
|   |   |   +-- xapi.py             # xAPI/cmi5 statement emitter
|   |   |   +-- routers/
|   |   |       +-- __init__.py     # Router package exports
|   |   |       +-- ranges.py       # Range CRUD + lifecycle (243 lines)
|   |   |       +-- exercises.py    # Exercise + scoring + AAR (303 lines)
|   |   |       +-- templates.py    # Template CRUD (126 lines)
|   |   |       +-- scenarios.py    # Scenario CRUD (134 lines)
|   |   |       +-- admin.py        # Tenants, users, teams, audit (222 lines)
|   |   +-- requirements.txt
|   |   +-- Dockerfile
|   |   +-- alembic/                # Database migrations
|   |   +-- alembic.ini
|   +-- web/                        # Angular 17+ frontend
|   |   +-- src/
|   |   |   +-- app/
|   |   |   |   +-- components/     # Shared components
|   |   |   |   +-- pages/          # Route pages (dashboard, ranges, exercises)
|   |   |   |   +-- services/       # HTTP and WebSocket services
|   |   |   |   +-- models/         # TypeScript interfaces
|   |   |   +-- environments/       # Environment configs
|   |   +-- angular.json
|   |   +-- package.json
|   +-- worker/                     # Celery background tasks
|       +-- worker/
|       |   +-- celery_app.py       # Celery configuration
|       |   +-- tasks.py            # Task definitions
|       +-- requirements.txt
|       +-- Dockerfile
+-- scenario-engine/
|   +-- injectors/
|   |   +-- base.py                 # Injector ABC + registry decorator
|   |   +-- dns_spike.py            # DNS query burst generator
|   |   +-- http_burst.py           # HTTP traffic generator
|   |   +-- email_phish.py          # Phishing email simulator
|   |   +-- simulated_execution.py  # Process/malware simulation
|   |   +-- identity.py             # Identity manipulation injector
|   +-- validators/
|   |   +-- base.py                 # Validator ABC + registry decorator
|   |   +-- opensearch_query.py     # OpenSearch detection checker
|   |   +-- manual_ack.py           # Manual acknowledgment validator
|   |   +-- deliverable_check.py    # MinIO artifact validator
|   +-- runner/
|   |   +-- run.py                  # CLI scenario executor
|   +-- schemas/
|   |   +-- scenario.schema.json    # JSON Schema for scenarios
|   |   +-- template.schema.json    # JSON Schema for templates
|   +-- examples/
|       +-- apt-breach.yaml         # APT attack chain scenario
|       +-- insider-threat.yaml     # Insider data exfiltration
|       +-- supply-chain.yaml       # Supply chain compromise
+-- ai-orchestrator/
|   +-- app/
|   |   +-- main.py                 # FastAPI app
|   |   +-- fleet.py                # Multi-node LLM fleet routing
|   |   +-- backends/               # OpenAI, Anthropic, Ollama, Mock
|   |   +-- prompts/                # System prompts per capability
|   +-- requirements.txt
|   +-- Dockerfile
+-- telemetry/
|   +-- pipelines/                  # OpenSearch ingest pipelines
|   +-- mappings/                   # Index templates
|   +-- dashboards/                 # Saved dashboard objects
+-- content/
|   +-- ranges/                     # Range template YAML files
|   +-- scenarios/                  # Scenario YAML files
|   +-- inject-packs/              # Reusable inject collections
|   +-- datasets/                   # Sample event data (JSON/NDJSON)
|   +-- detections/                 # Sigma-style detection rules
+-- infra/
|   +-- platform/docker/
|   |   +-- compose.dev.yml         # Core development services
|   |   +-- compose.helk.yml        # HELK overlay
|   |   +-- compose.velociraptor.yml # Velociraptor overlay
|   +-- proxmox/
|       +-- terraform/              # Terraform modules
|       +-- packer/                 # Packer templates
+-- tools/
|   +-- cli/
|   |   +-- forge.py                # Typer CLI (range, exercise, template commands)
|   |   +-- requirements.txt
|   +-- sdk/
|       +-- __init__.py
|       +-- client.py               # TrueNorthClient class
+-- tests/
|   +-- conftest.py                 # Shared fixtures (db, client, auth mock)
|   +-- api/                        # API endpoint tests
|   +-- scenario_engine/            # Injector/validator tests
|   +-- worker/                     # Task tests
+-- scripts/
|   +-- dod.ps1                     # Windows DoD gate script
|   +-- dod.sh                      # Linux/macOS DoD gate
+-- docs/                           # Documentation
+-- AGENTS.md                       # Agent operating system
+-- SKILLS.md                       # Skill index
```

---

## Local Setup

### Step 1: Clone the Repository

```bash
git clone <repo-url>
cd "TrueNorth Range/Core_Docs"
```

### Step 2: Start Infrastructure Services

```bash
cd infra/platform/docker
docker compose -f compose.dev.yml up -d

# Wait for all services to be healthy
docker compose -f compose.dev.yml ps
```

### Step 3: Set Up Python Virtual Environment

```bash
# API
cd control-plane/api
python -m venv .venv

# Activate (choose your OS)
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\Activate.ps1       # Windows PowerShell
.venv\Scripts\activate.bat       # Windows CMD

pip install -r requirements.txt
```

### Step 4: Run the API Server

```bash
cd control-plane/api
AUTH_DISABLED=true uvicorn app.main:app --reload --port 8080
```

The API is now available at `http://localhost:8080`. Auto-reload is enabled for development.

### Step 5: Run the Celery Worker (Optional)

```bash
cd control-plane/worker
pip install -r requirements.txt
celery -A worker.celery_app worker --loglevel=info
```

### Step 6: Verify Setup

```bash
# Health check
curl http://localhost:8080/health

# Interactive API docs
open http://localhost:8080/docs

# CLI
pip install -r tools/cli/requirements.txt
python tools/cli/forge.py health
```

---

## Running Tests

### Full Test Suite

```bash
# Install test dependencies
pip install pytest pytest-cov pyyaml httpx

# Run all tests
pytest -v

# With coverage report
pytest --cov=control_plane_api --cov-report=html --cov-report=term -v

# Open coverage report
open htmlcov/index.html
```

### Test Categories

```bash
# API endpoint tests
pytest tests/api/ -v

# Scenario engine tests (injectors, validators)
pytest tests/scenario_engine/ -v

# Content validation tests
pytest tests/scenario_engine/test_content_validation.py -v

# Worker/task tests
pytest tests/worker/ -v

# Single test file
pytest tests/api/test_ranges.py -v

# Single test function
pytest tests/api/test_ranges.py::test_create_range -v
```

### Load Testing

```bash
# Install locust
pip install locust

# Run load tests
locust -f tests/load/locustfile.py --host=http://localhost:8080

# Headless mode (CI)
locust -f tests/load/locustfile.py \
  --host=http://localhost:8080 \
  --users 100 \
  --spawn-rate 10 \
  --run-time 60s \
  --headless
```

### Test Coverage Requirements

| Module | Minimum Coverage |
|--------|-----------------|
| API routers | 85% |
| Models/schemas | 90% |
| RBAC | 95% |
| Scenario engine | 80% |
| Worker tasks | 75% |
| Overall | 80% |

---

## Code Style and Linting

### Ruff (Linting + Formatting)

```bash
# Check for lint errors
ruff check .

# Auto-fix lint errors
ruff check . --fix

# Format code
ruff format .

# Check formatting (CI mode)
ruff format . --check
```

**Ruff configuration** (`pyproject.toml`):

```toml
[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM", "TCH"]
ignore = ["E501"]  # Line length handled by formatter

[tool.ruff.lint.isort]
known-first-party = ["app", "worker", "scenario_engine"]
```

### Mypy (Type Checking)

```bash
mypy control-plane/api/app --ignore-missing-imports
```

### Pre-commit Hooks

```bash
# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

**`.pre-commit-config.yaml`:**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.9.0
    hooks:
      - id: mypy
        additional_dependencies: [sqlalchemy-stubs]
```

---

## Database Migrations

TrueNorth Range uses Alembic for database schema migrations.

### Setup (First Time)

```bash
cd control-plane/api
pip install alembic

# Initialize (already done — alembic/ directory exists)
alembic init alembic
```

### Creating Migrations

```bash
# Auto-generate from model changes
alembic revision --autogenerate -m "Add user_preferences table"

# Manual migration
alembic revision -m "Seed initial data"
```

### Applying Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Upgrade to specific revision
alembic upgrade abc123

# Rollback one step
alembic downgrade -1

# Rollback to specific revision
alembic downgrade abc123

# View migration history
alembic history

# View current revision
alembic current
```

### Migration Best Practices

1. **Always review** auto-generated migrations before applying
2. **Test migrations** against a fresh database AND existing data
3. **Include both** upgrade and downgrade functions
4. **Never modify** an already-applied migration
5. **Add data migrations** as separate revisions from schema changes

---

## Seed Data and Content Import

### Where seeding happens

`control-plane/api/app/seed.py` seeds **reference data only** — nations, coalitions,
auth zones, infrastructure, and AI backends. It deliberately seeds **no courses and no
scenarios**; content carries provenance and enters through an importer, never through a
Python literal.

It is called from `_seed_dev_data()` in `app/main.py` on **every** startup, from the
`lifespan` handler. There is no env flag gating it — each seed function is itself
responsible for returning early when its table is already populated, which is why
idempotency is mandatory rather than a nicety.

```python
from .seed import (
    seed_ai_backends,
    seed_auth_zones,
    seed_infrastructure,
    seed_nations_and_coalitions,
)
```

> **The import root is `app`, not `control_plane.api.app`.** `pytest.ini` puts
> `control-plane/api` on the path. The repository directory is `control-plane`
> (hyphenated), which is **not** a valid Python package name — `import control_plane`
> can never work. Inside the API package use relative imports (`from .seed import ...`);
> from tests use `from app import seed`.

`_seed_dev_data()` wraps its work in `try/except Exception` and logs
`"Seed failed (may already exist)"`. That swallows `ImportError` too, so a broken seed
import shows up as a benign-looking warning rather than a crash. `tests/api/test_seed_integrity.py`
exists specifically to make that failure loud — it asserts every seed function `main.py`
imports actually exists.

### Adding a seed function

1. Add the function to `seed.py`, taking a single `Session` argument.
2. Make it **idempotent** — return early if its table is already populated.
3. Import **and call** it in `_seed_dev_data()`; importing without calling is a silent no-op.
4. Add its name to `REQUIRED_SEED_FUNCTIONS` in `tests/api/test_seed_integrity.py`.

### Importing content (do not seed it)

Content enters through deterministic CSV importers so it stays auditable for
accreditation. Each is idempotent (upsert on a natural key):

| Content | Endpoint | Source file |
|---|---|---|
| QSP/CFITES spine (Qualification → PO → EO) | `POST /api/v1/qsp/import-crosswalk` | `truenorth-content-pack/truenorth-content/crosswalk.csv` |
| NICE/CSF competency crosswalk | `POST /api/v1/qsp/import-competency-crosswalk` | `content/catalogue/qsp_competency_crosswalk.csv` + `nist_csf_2_0_taxonomy.csv` |
| Golden-image catalogue | `POST /api/v1/golden-images/import-catalogue` | `content/catalogue/vm_iso_catalogue.csv` |
| Academic programme catalogue | `POST /api/v1/courses/import-programme` | `content/catalogue/cyber_operator_programme.csv` |
| Authored course content (modules, labs, quizzes) | `POST /api/v1/courses/import-course-content` | `content/courses/*.yaml` |

```bash
curl -F 'file=@content/catalogue/cyber_operator_programme.csv' \
     -H "Authorization: Bearer $TOKEN" \
     http://localhost:8080/api/v1/courses/import-programme
```

Each importer follows the same shape: a **pure** `parse_*(csv_text) -> list[dict]`
function with no DB access (so it is unit-testable), plus an `import_*(db, csv_text)`
that upserts. Copy `app/qsp_ingest.py` or `app/programme_ingest.py` when adding another.

### Citing sources in course content

Course files under `content/courses/` cite sources by **key**, not by free text:

```yaml
modules:
  - ordinal: 1
    refs: [nist-sp-800-183, owasp-iot]
```

Every key must exist in `content/catalogue/references.yaml`, and
`tests/api/test_course_content_ingest.py` fails the build if one does not.

**Do not add a reference without opening its URL and confirming the title and year.**
This rule exists because the draft this content grew from invented citations: it cited
NISTIR 8286 as "Securing the Internet of Things" (it is *Integrating Cybersecurity and
Enterprise Risk Management*), NIST SP 800-183 as "IoT Threat Landscape 2024" (it is
*Networks of 'Things'*, 2016), and OWASP IoT Top 10 items as "T1/T9" (they are I1–I10,
and I9 is Insecure Default Settings, not Insecure Network Services — that is I2).

Watch for withdrawn publications. NIST SP 800-61 Rev. 2 was withdrawn on 2025-04-03 and
superseded by Rev. 3; SP 800-63-3 is superseded by 800-63-4. A test asserts neither is
cited.

Loading the whole library into a dev database:

```bash
curl -F "file=@content/catalogue/cyber_operator_programme.csv" \
     -H "Authorization: Bearer $TOKEN" \
     http://localhost:8080/api/v1/courses/import-programme

for f in content/courses/*.yaml; do
  curl -F "file=@$f" -H "Authorization: Bearer $TOKEN" \
       http://localhost:8080/api/v1/courses/import-course-content
done
```

Both importers are idempotent, so re-running is safe.

### Putting content on the developmental path

The developmental path — what `GET /api/v1/qsp/curriculum-map` renders — is built from
the **QSP spine**, not from the course catalogue. Its nodes are `Qualification` rows
laid out in DP columns by `dp_order`; the objectives on each node are
`PerformanceObjective` rows from `crosswalk.csv`. Learner position is resolved by
`qsp_progress`, which walks exactly one link:

```
PerformanceObjective <- CourseModule.po_id -> Course <- Enrollment -> ModuleProgress
```

**`CourseModule.po_id` is the only way authored content reaches the path.** A course
with no mapped module contributes nothing to the map and nothing to progress, however
good its content is.

Mapping is declared per module, and is never inferred:

```yaml
modules:
  - ordinal: 1
    title: Log Sources and Collection
    po: {qsp_code: ALJQ, po_code: PO_009}   # optional
```

A course may also declare a top-level `qsp_code`, which sets `Course.qualification_id`.

Rules the importer enforces:

- A declared PO that does not exist in the ingested spine is a **422, not a silent
  skip** — a dropped mapping would leave the module looking wired up while delivering
  nothing.
- Mapping one module never causes the others to be guessed.
- `tests/api/test_developmental_path_binding.py` asserts that any `po:` appearing in a
  course file corresponds to a real row in `crosswalk.csv`.

**The shipped mapping is PROPOSED, not Standards-validated.** Asserting that a module
satisfies a performance objective is a CFITES claim that partly determines whether a CAF
member is certified qualified, so the current mapping is a reviewable starting point for
Standards, not an accreditation decision. Every course file records that in
`source.notes`, and a test asserts it still says so.

15 of the 16 objectives have a delivering module. The exception is `TEMP67 / PO_TODO`,
a `needs_spec` placeholder for the remaining Cpl POs — there is nothing real to deliver
against, and a test asserts it stays the only gap.

**One module per objective.** `qsp_progress` takes the first module it finds for a PO, so
a second mapping to the same objective is silently ignored and that course would not
count toward progress. A test enforces one-to-one.

To see what still needs mapping:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/qsp/po-coverage
```

which reports, per objective, whether any module delivers it, plus how many modules are
still unbound.

Separately, the academic programme has its own **delivery** paths, generated from the
catalogue and carrying no qualification meaning:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     http://localhost:8080/api/v1/courses/generate-programme-paths
```

This creates one unpublished `LearningPath` per term plus one per programme/DP. These
sit alongside the CFITES paths from `qsp_paths.generate_learning_paths`; they say only
"these courses are taught in this order".

### Provenance rules

Content whose source cannot be cited is imported **unpublished** and **unbound** from
the qualification spine, and records `provenance` in `course_meta`. See
`app/programme_ingest.py` — the cyber-operator programme catalogue is entirely
`provenance=unsourced`, and `tests/api/test_programme_ingest.py` enforces that none of
it can reach a published or spine-bound state.

This mirrors the norm stated in `app/qsp_paths.py`: rungs with no CAF source carry no
rank label rather than a guessed one, because *fabricated data must never reach CAF
users*. Never "correct" invented data into apparent legitimacy — replace it with a
sourced file.

---

## Adding New API Endpoints

### Step-by-Step Guide

**1. Add the model** (`control-plane/api/app/models.py`):

```python
class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_tenant", "tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(50), default="info")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
```

**2. Add schemas** (`control-plane/api/app/schemas.py`):

```python
class NotificationIn(BaseModel):
    message: str
    severity: str = "info"

class NotificationOut(BaseModel):
    id: uuid.UUID
    message: str
    severity: str
    read: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

**3. Add permissions** (`control-plane/api/app/rbac.py`):

```python
class Permission(str, Enum):
    # ... existing permissions ...
    NOTIFICATION_CREATE = "notification:create"
    NOTIFICATION_READ = "notification:read"
```

**4. Create router** (`control-plane/api/app/routers/notifications.py`):

```python
router = APIRouter(prefix="/notifications", tags=["notifications"])

@router.post("", response_model=NotificationOut, status_code=201)
def create_notification(
    body: NotificationIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.NOTIFICATION_CREATE)),
) -> Notification:
    notif = Notification(
        tenant_id=uuid.UUID(user.tenant_id),
        message=body.message,
        severity=body.severity,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif
```

**5. Register router** (`control-plane/api/app/main.py`):

```python
from .routers.notifications import router as notifications_router
app.include_router(notifications_router)
```

**6. Generate migration:**

```bash
alembic revision --autogenerate -m "Add notifications table"
alembic upgrade head
```

**7. Write tests** (`tests/api/test_notifications.py`):

```python
def test_create_notification(client, auth_headers):
    response = client.post("/notifications", json={"message": "Test"}, headers=auth_headers)
    assert response.status_code == 201
    assert response.json()["message"] == "Test"
```

---

## Adding New Scenario Injectors

### Step-by-Step Guide

**1. Create injector file** (`scenario-engine/injectors/my_injector.py`):

```python
from scenario_engine.injectors.base import Injector, register_injector, RangeContext, InjectResult

@register_injector("lateral_movement")
class LateralMovementInjector(Injector):
    """Simulates lateral movement via PsExec or WMI."""

    def execute(self, context: RangeContext, params: dict) -> InjectResult:
        technique = params.get("technique", "T1570")
        source = params.get("source_host", "ws01")
        target = params.get("target_host", "srv01")
        method = params.get("method", "psexec")

        # Generate telemetry events
        events = [
            {
                "timestamp": context.current_time.isoformat(),
                "event.action": "network_connection",
                "source.ip": context.resolve_host(source),
                "destination.ip": context.resolve_host(target),
                "destination.port": 445,
                "process.name": f"{method}.exe",
            },
            {
                "timestamp": context.current_time.isoformat(),
                "event.action": "process_create",
                "host.name": target,
                "process.name": "cmd.exe",
                "process.parent.name": f"{method}.exe",
            },
        ]

        return InjectResult(
            success=True,
            message=f"Lateral movement: {source} -> {target} via {method}",
            artifacts={"events": events},
            mitre_technique=technique,
        )
```

**2. Add to injector registry** — The `@register_injector` decorator handles this automatically.

**3. Write tests** (`tests/scenario_engine/test_lateral_movement.py`):

```python
def test_lateral_movement_injector():
    from scenario_engine.injectors.my_injector import LateralMovementInjector
    injector = LateralMovementInjector()
    result = injector.execute(mock_context, {"source_host": "ws01", "target_host": "srv01"})
    assert result.success
    assert len(result.artifacts["events"]) == 2
```

**4. Use in scenarios:**

```yaml
timeline:
  - id: event-3
    type: lateral_movement
    delay_minutes: 15
    depends_on: event-2
    params:
      source_host: ws01
      target_host: srv01
      method: psexec
      technique: T1570
```

---

## Adding New Packer Templates

### Step-by-Step Guide

**1. Create Packer template** (`infra/proxmox/packer/windows-server-2022.pkr.hcl`):

```hcl
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

source "proxmox-iso" "windows-server-2022" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  token                    = var.proxmox_token
  node                     = var.proxmox_node
  iso_file                 = "local:iso/windows-server-2022.iso"
  iso_storage_pool         = "local"
  vm_name                  = "tpl-windows-server-2022"
  template_name            = "tpl-windows-server-2022"
  os                       = "win11"
  cores                    = 4
  memory                   = 8192
  disk_size                = "80G"
  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }
}

build {
  sources = ["source.proxmox-iso.windows-server-2022"]

  provisioner "powershell" {
    scripts = [
      "scripts/install-sysmon.ps1",
      "scripts/install-filebeat.ps1",
      "scripts/harden.ps1",
    ]
  }
}
```

**2. Build the template:**

```bash
cd infra/proxmox/packer
packer init .
packer validate windows-server-2022.pkr.hcl
packer build windows-server-2022.pkr.hcl
```

**3. Reference in range templates:**

```yaml
vms:
  - name: dc01
    template: tpl-windows-server-2022
    role: domain-controller
    cpu: 4
    memory: 8192
```

---

## Frontend Development

### Setup

```bash
cd control-plane/web
npm install

# Start development server with API proxy
ng serve --proxy-config proxy.conf.json
```

**`proxy.conf.json`:**

```json
{
  "/api": {
    "target": "http://localhost:8080",
    "secure": false,
    "pathRewrite": { "^/api": "" }
  },
  "/ws": {
    "target": "ws://localhost:8080",
    "ws": true
  }
}
```

### Frontend Architecture

```
src/app/
+-- components/           # Shared UI components
|   +-- range-card/
|   +-- exercise-timer/
|   +-- objective-list/
+-- pages/                # Route pages
|   +-- dashboard/
|   +-- ranges/
|   +-- exercises/
|   +-- scenarios/
|   +-- aar-viewer/
+-- services/             # API and WebSocket services
|   +-- api.service.ts
|   +-- websocket.service.ts
|   +-- auth.service.ts
+-- models/               # TypeScript interfaces
|   +-- range.model.ts
|   +-- exercise.model.ts
+-- guards/               # Route guards
|   +-- auth.guard.ts
|   +-- role.guard.ts
```

### Build for Production

```bash
ng build --configuration production
# Output: dist/truenorth-web/
```

---

## Docker Development Workflow

### Rebuild After Code Changes

```bash
# Rebuild specific service
docker compose -f compose.dev.yml build api
docker compose -f compose.dev.yml up -d api

# Rebuild all
docker compose -f compose.dev.yml build
docker compose -f compose.dev.yml up -d
```

### View Logs

```bash
# All services
docker compose -f compose.dev.yml logs -f

# Specific service
docker compose -f compose.dev.yml logs -f api worker

# Last 100 lines
docker compose -f compose.dev.yml logs --tail=100 api
```

### Shell Into Container

```bash
docker compose -f compose.dev.yml exec api bash
docker compose -f compose.dev.yml exec postgres psql -U truenorth truenorth_range
docker compose -f compose.dev.yml exec redis redis-cli
```

### Reset Everything

```bash
docker compose -f compose.dev.yml down -v  # Remove volumes too
docker compose -f compose.dev.yml up -d     # Fresh start
```

---

## Debugging Tips

### API Debugging

```bash
# Enable debug logging
LOG_LEVEL=DEBUG AUTH_DISABLED=true uvicorn app.main:app --reload --port 8080

# Use debugger (VS Code)
# Add to launch.json:
# {
#   "name": "FastAPI",
#   "type": "python",
#   "request": "launch",
#   "module": "uvicorn",
#   "args": ["app.main:app", "--reload", "--port", "8080"],
#   "env": {"AUTH_DISABLED": "true"}
# }
```

### Database Debugging

```bash
# Connect to database
docker exec -it tn-postgres psql -U truenorth truenorth_range

# Useful queries
SELECT state, count(*) FROM ranges GROUP BY state;
SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 20;
SELECT * FROM exercises WHERE state = 'running';
```

### Celery Debugging

```bash
# Monitor tasks in real-time
celery -A worker.celery_app events

# Inspect active tasks
celery -A worker.celery_app inspect active

# Inspect registered tasks
celery -A worker.celery_app inspect registered
```

### WebSocket Debugging

```bash
# Use websocat for WebSocket testing
websocat ws://localhost:8080/ws

# Or with Python
python -c "
import asyncio, websockets
async def test():
    async with websockets.connect('ws://localhost:8080/ws') as ws:
        msg = await ws.recv()
        print(msg)
asyncio.run(test())
"
```

---

## Development Workflow

Follow the AGENTS.md operating model:

### 1. PLAN
- Review requirements and acceptance criteria
- Check `SKILLS/` directory for relevant guidelines
- Identify affected files and components

### 2. IMPLEMENT
- Follow `SKILLS/30-implementation-rules.md` coding standards
- Write code with comprehensive docstrings
- Add audit logging for new operations
- Ensure tenant isolation for new data models

### 3. VERIFY
- Run unit tests: `pytest -v`
- Run linter: `ruff check .`
- Run type checker: `mypy`
- Run DoD gate: `pwsh scripts/dod.ps1`

### 4. REVIEW
- Self-review against `SKILLS/50-pr-review.md`
- Check for security issues (SQL injection, XSS, auth bypass)
- Verify test coverage meets thresholds
- Commit with a descriptive message

---

## CLI Tool Reference

```bash
# Install CLI dependencies
pip install -r tools/cli/requirements.txt

# General
python tools/cli/forge.py health              # Check API health
python tools/cli/forge.py seed --demo          # Seed demo data

# Ranges
python tools/cli/forge.py range list           # List all ranges
python tools/cli/forge.py range create --name "R1" --template-id <uuid>
python tools/cli/forge.py range provision <id>
python tools/cli/forge.py range destroy <id>

# Templates
python tools/cli/forge.py template list
python tools/cli/forge.py template validate <yaml-path>

# Exercises
python tools/cli/forge.py exercise list
python tools/cli/forge.py exercise start <id>
python tools/cli/forge.py exercise complete <id>
```

---

## SDK Usage

```python
from tools.sdk import TrueNorthClient

# Initialize
client = TrueNorthClient(
    base_url="http://localhost:8080",
    token="<jwt-token>"  # Optional if AUTH_DISABLED=true
)

# Health check
print(client.health())

# Range operations
ranges = client.list_ranges()
new_range = client.create_range(name="SDK Range", template_id="<uuid>")
client.provision_range(new_range["id"])

# Exercise operations
exercises = client.list_exercises()
client.start_exercise(exercise_id="<uuid>")
client.complete_exercise(exercise_id="<uuid>")

# Telemetry
events = client.search_telemetry(range_id="<uuid>", query="process.name:powershell.exe")
```