# TrueNorth Range — Architecture

> Comprehensive architecture documentation covering system design, component interactions, data flows, technology decisions, and scalability strategies.

---

## Table of Contents

- [System Context](#system-context)
- [Container Diagram](#container-diagram)
- [Component Diagrams](#component-diagrams)
- [Data Flow Diagrams](#data-flow-diagrams)
- [Data Model](#data-model)
- [Technology Decisions](#technology-decisions)
- [Scalability Design](#scalability-design)
- [Multi-Tenancy Model](#multi-tenancy-model)
- [Network Architecture](#network-architecture)
- [AI Architecture](#ai-architecture)
- [Security Architecture](#security-architecture)

---

## System Context

TrueNorth Range sits at the center of a cyber training ecosystem, interacting with human users, external identity providers, AI model backends, and virtualization infrastructure.

```mermaid
C4Context
    title System Context Diagram — TrueNorth Range

    Person(instructor, "Instructor", "Creates ranges, designs scenarios, runs exercises")
    Person(trainee, "Trainee / Student", "Participates in exercises, responds to incidents")
    Person(admin, "Platform Admin", "Manages tenants, users, infrastructure")
    Person(observer, "Observer", "Views exercise progress, telemetry, AARs")

    System(tnr, "TrueNorth Range", "AI-enhanced cyber training platform")

    System_Ext(keycloak, "Keycloak", "OIDC Identity Provider")
    System_Ext(proxmox, "Proxmox VE", "Virtualization cluster")
    System_Ext(llm, "LLM Backends", "OpenAI / Anthropic / Ollama")
    System_Ext(lrs, "Learning Record Store", "xAPI/cmi5 consumer")

    Rel(instructor, tnr, "Manages ranges, scenarios, exercises")
    Rel(trainee, tnr, "Participates in exercises")
    Rel(admin, tnr, "Administers platform")
    Rel(observer, tnr, "Views live data and reports")
    Rel(tnr, keycloak, "Authenticates users via OIDC")
    Rel(tnr, proxmox, "Provisions VMs via Terraform")
    Rel(tnr, llm, "Generates detections, suggestions, AAR analysis")
    Rel(tnr, lrs, "Emits xAPI statements")
```

---

## Container Diagram

The platform consists of 12+ deployable containers organized into four layers: client, API, processing, and data.

```mermaid
C4Container
    title Container Diagram — TrueNorth Range

    Person(user, "User", "Instructor / Trainee / Admin")

    Container_Boundary(client, "Client Layer") {
        Container(web, "Angular Frontend", "Angular 17+, Material M3", "SPA with dashboard, range mgmt, scenario builder")
        Container(cli, "forge.py CLI", "Python, Typer", "Command-line management tool")
        Container(sdk, "Python SDK", "Python", "Programmatic API client")
    }

    Container_Boundary(api, "API Layer") {
        Container(cpapi, "Control Plane API", "FastAPI 0.115+, Pydantic v2", "42 REST endpoints + WebSocket")
        Container(wsm, "WebSocket Manager", "asyncio, Redis pub/sub", "Real-time event broadcasting")
    }

    Container_Boundary(processing, "Processing Layer") {
        Container(worker, "Celery Worker Pool", "Celery 5.3+, Redis", "Async task execution")
        Container(scenario, "Scenario Engine", "Python", "Injectors + validators + runner")
        Container(ai, "AI Orchestrator", "FastAPI, port 6000", "LLM fleet routing + capabilities")
    }

    Container_Boundary(data, "Data Layer") {
        ContainerDb(pg, "PostgreSQL", "PostgreSQL 16", "Relational state, 12 tables")
        ContainerDb(redis, "Redis", "Redis 7", "Task broker, WS pub/sub, cache")
        ContainerDb(os, "OpenSearch", "OpenSearch 2.13", "Telemetry indices, search")
        ContainerDb(minio, "MinIO", "S3-compatible", "Artifacts, reports, images")
    }

    Container_Boundary(infra, "Infrastructure Layer") {
        Container(tf, "Terraform", "HCL, Proxmox provider", "VM provisioning")
        Container(packer, "Packer", "HCL", "VM template building")
    }

    Rel(user, web, "HTTPS")
    Rel(user, cli, "Terminal")
    Rel(web, cpapi, "REST/WS")
    Rel(cli, cpapi, "REST")
    Rel(sdk, cpapi, "REST")
    Rel(cpapi, pg, "SQLAlchemy")
    Rel(cpapi, redis, "Cache/pub-sub")
    Rel(cpapi, os, "HTTP")
    Rel(cpapi, minio, "S3 API")
    Rel(cpapi, worker, "Celery tasks")
    Rel(worker, tf, "CLI")
    Rel(worker, scenario, "Python import")
    Rel(worker, ai, "HTTP")
    Rel(ai, os, "HTTP queries")
```

---

## Component Diagrams

### Control Plane API

The API is a FastAPI application organized into 5 router modules with shared authentication, RBAC, and database layers.

```mermaid
graph TB
    subgraph "Control Plane API (FastAPI)"
        MAIN["main.py<br/>App factory + middleware"]
        AUTH["auth.py<br/>Keycloak JWT validation"]
        RBAC["rbac.py<br/>25+ permissions, 5 roles"]
        DB["db.py<br/>SQLAlchemy engine + sessions"]
        MODELS["models.py<br/>12 ORM models"]
        SCHEMAS["schemas.py<br/>Pydantic v2 schemas"]
        WSM["websocket_manager.py<br/>Redis pub/sub, heartbeat"]
        XAPI["xapi.py<br/>xAPI statement emitter"]

        subgraph "Routers"
            R_RANGES["ranges.py<br/>10 endpoints"]
            R_EXERCISES["exercises.py<br/>11 endpoints"]
            R_TEMPLATES["templates.py<br/>5 endpoints"]
            R_SCENARIOS["scenarios.py<br/>5 endpoints"]
            R_ADMIN["admin.py<br/>11 endpoints"]
        end
    end

    MAIN --> AUTH
    MAIN --> R_RANGES
    MAIN --> R_EXERCISES
    MAIN --> R_TEMPLATES
    MAIN --> R_SCENARIOS
    MAIN --> R_ADMIN
    R_RANGES --> RBAC
    R_EXERCISES --> RBAC
    R_TEMPLATES --> RBAC
    R_SCENARIOS --> RBAC
    R_ADMIN --> RBAC
    RBAC --> AUTH
    R_RANGES --> DB
    R_EXERCISES --> DB
    R_RANGES --> MODELS
    R_EXERCISES --> MODELS
    MODELS --> DB
    R_RANGES --> WSM
    R_EXERCISES --> WSM
    MAIN --> XAPI
```

### Worker Pool

```mermaid
graph TB
    subgraph "Celery Worker Pool"
        APP["celery_app.py<br/>Configuration + discovery"]
        TASKS["tasks.py<br/>Task definitions"]
        
        subgraph "Task Types"
            PROV["provision_range<br/>Terraform apply or mock"]
            DEST["destroy_range<br/>Terraform destroy or mock"]
            BATCH["batch_provision<br/>Parallel range provisioning"]
            SCEN["run_scenario<br/>Execute scenario timeline"]
        end
    end

    subgraph "Backends"
        MOCK["Mock Provisioner<br/>State transitions only"]
        TF["Terraform Backend<br/>Proxmox provider"]
        SE["Scenario Engine<br/>Injectors + validators"]
    end

    APP --> TASKS
    TASKS --> PROV
    TASKS --> DEST
    TASKS --> BATCH
    TASKS --> SCEN
    PROV --> MOCK
    PROV --> TF
    DEST --> MOCK
    DEST --> TF
    SCEN --> SE
```

### AI Orchestrator

```mermaid
graph TB
    subgraph "AI Orchestrator (Port 6000)"
        AIMAIN["main.py<br/>FastAPI app"]
        FLEET["fleet.py<br/>Multi-node routing"]
        
        subgraph "Capabilities"
            DET["Detection Rule Gen<br/>/ai/detection-rule"]
            SUG["Scenario Suggestions<br/>/ai/scenario-suggest"]
            AAR["AAR Analysis<br/>/ai/aar-analysis"]
            GEN["General Generate<br/>/ai/generate"]
        end

        subgraph "Backends"
            OAI["OpenAI Backend"]
            ANT["Anthropic Backend"]
            OLL["Ollama Backend<br/>Local models"]
            MOCK_AI["Mock Backend<br/>Static responses"]
        end

        subgraph "Prompts"
            P1["detection_system.txt"]
            P2["scenario_system.txt"]
            P3["aar_system.txt"]
        end
    end

    AIMAIN --> FLEET
    AIMAIN --> DET
    AIMAIN --> SUG
    AIMAIN --> AAR
    AIMAIN --> GEN
    FLEET --> OAI
    FLEET --> ANT
    FLEET --> OLL
    FLEET --> MOCK_AI
    DET --> P1
    SUG --> P2
    AAR --> P3
```

### Scenario Engine

```mermaid
graph TB
    subgraph "Scenario Engine"
        RUNNER["runner/run.py<br/>CLI executor"]
        PARSER["YAML parser<br/>Timeline + objectives"]

        subgraph "Injector Registry"
            I_DNS["dns_spike.py<br/>DNS burst"]
            I_HTTP["http_burst.py<br/>HTTP traffic"]
            I_PHISH["email_phish.py<br/>Phishing email"]
            I_EXEC["simulated_execution.py<br/>Process sim"]
            I_IDENT["identity.py<br/>Admin account"]
        end

        subgraph "Validator Registry"
            V_OS["opensearch_query.py<br/>Event detection"]
            V_ACK["manual_ack.py<br/>Human confirm"]
            V_DEL["deliverable_check.py<br/>Artifact upload"]
        end
    end

    RUNNER --> PARSER
    PARSER --> I_DNS
    PARSER --> I_HTTP
    PARSER --> I_PHISH
    PARSER --> I_EXEC
    PARSER --> I_IDENT
    PARSER --> V_OS
    PARSER --> V_ACK
    PARSER --> V_DEL
```

---

## Data Flow Diagrams

### Range Provisioning Flow

```mermaid
sequenceDiagram
    participant U as User (Instructor)
    participant API as Control Plane API
    participant DB as PostgreSQL
    participant Q as Redis (Celery)
    participant W as Celery Worker
    participant TF as Terraform/Mock
    participant WS as WebSocket

    U->>API: POST /ranges (name, template_id)
    API->>DB: INSERT range (state=created)
    API-->>U: 201 Created (range object)

    U->>API: POST /ranges/{id}/provision
    API->>DB: UPDATE state=provisioning
    API->>Q: Enqueue provision_range task
    API->>WS: Broadcast range_state=provisioning
    API-->>U: 200 OK

    Q->>W: Dequeue task
    W->>TF: terraform apply (or mock delay)
    TF-->>W: Apply complete (outputs)
    W->>DB: UPDATE state=ready, provisioner_output
    W->>WS: Broadcast range_state=ready
```

### Exercise Lifecycle Flow

```mermaid
sequenceDiagram
    participant I as Instructor
    participant API as Control Plane API
    participant DB as PostgreSQL
    participant W as Celery Worker
    participant SE as Scenario Engine
    participant OS as OpenSearch
    participant T as Trainee
    participant WS as WebSocket

    I->>API: POST /exercises (range_id, scenario_id)
    API->>DB: INSERT exercise (state=pending)
    API-->>I: 201 Created

    I->>API: POST /exercises/{id}/start
    API->>DB: UPDATE state=running, started_at
    API->>W: run_scenario task
    API->>WS: Broadcast exercise_update
    API-->>I: 200 OK

    W->>SE: Execute timeline events
    loop For each timeline event
        SE->>OS: Inject telemetry events
        SE->>WS: Broadcast scenario_event
    end

    T->>API: GET /exercises/{id}/objectives
    API-->>T: Objective list

    T->>API: POST /exercises/{id}/objectives/{ref}/ack
    API->>DB: UPDATE objective (achieved=true)
    API->>WS: Broadcast exercise_update

    I->>API: POST /exercises/{id}/complete
    API->>DB: Tally scores, state=completed
    API-->>I: Exercise with final scores
```

### Telemetry Pipeline Flow

```mermaid
graph LR
    subgraph "Range VMs"
        SM["Sysmon"]
        ZK["Zeek"]
        SU["Suricata"]
        FB["Filebeat"]
    end

    subgraph "Ingestion"
        INJ["Scenario Injectors"]
        API_TEL["POST /telemetry/{range_id}/events"]
    end

    subgraph "OpenSearch Cluster"
        IDX["Per-range Index<br/>tn-range-{uuid}-*"]
        PIPE["Ingest Pipeline<br/>tenant_id, range_id tagging"]
        DASH["OpenSearch Dashboards"]
    end

    subgraph "Optional: HELK"
        KFK["Kafka"]
        LS["Logstash"]
        ES["Elasticsearch"]
        KIB["Kibana"]
        SPK["Spark / Jupyter"]
    end

    SM --> FB
    ZK --> FB
    SU --> FB
    FB --> PIPE
    INJ --> API_TEL
    API_TEL --> PIPE
    PIPE --> IDX
    IDX --> DASH
    FB --> KFK
    KFK --> LS
    LS --> ES
    ES --> KIB
    KFK --> SPK
```

---

## Data Model

The relational data model consists of 12 tables organized around tenant isolation and range/exercise lifecycle management.

```mermaid
erDiagram
    TENANTS ||--o{ USERS : "has"
    TENANTS ||--o{ TEAMS : "has"
    TENANTS ||--o{ RANGES : "owns"
    TENANTS ||--o{ TEMPLATES : "owns"
    TENANTS ||--o{ SCENARIOS : "owns"
    TENANTS ||--o{ EXERCISES : "owns"
    TENANTS ||--o{ AUDIT_LOGS : "generates"

    USERS ||--o{ TEAM_MEMBERSHIPS : "belongs to"
    TEAMS ||--o{ TEAM_MEMBERSHIPS : "contains"

    TEMPLATES ||--o{ RANGES : "instantiates"
    SCENARIOS ||--o{ EXERCISES : "defines"
    RANGES ||--o{ EXERCISES : "hosts"
    EXERCISES ||--o{ OBJECTIVES : "has"
    EXERCISES ||--o| AFTER_ACTION_REPORTS : "generates"

    TENANTS {
        uuid id PK
        string name UK
        string slug UK
        bool is_active
        timestamp created_at
        timestamp updated_at
    }

    USERS {
        uuid id PK
        string keycloak_id UK
        string email UK
        string display_name
        enum role "admin/instructor/student/observer/range_ops"
        uuid tenant_id FK
        bool is_active
    }

    TEAMS {
        uuid id PK
        string name
        uuid tenant_id FK
    }

    TEAM_MEMBERSHIPS {
        uuid id PK
        uuid user_id FK
        uuid team_id FK
        string role
    }

    TEMPLATES {
        uuid id PK
        string name
        string version
        text yaml
        uuid tenant_id FK
        bool is_public
    }

    SCENARIOS {
        uuid id PK
        string name
        string version
        text yaml
        uuid tenant_id FK
        bool is_public
    }

    RANGES {
        uuid id PK
        string name
        uuid template_id FK
        enum state "created/provisioning/ready/running/stopped/destroying/destroyed/failed"
        uuid tenant_id FK
        string provisioner_backend
        text provisioner_output
        text error_message
    }

    EXERCISES {
        uuid id PK
        string name
        uuid range_id FK
        uuid scenario_id FK
        enum state "pending/running/paused/completed/cancelled"
        uuid tenant_id FK
        timestamp started_at
        timestamp completed_at
        int total_score
        int max_score
    }

    OBJECTIVES {
        uuid id PK
        uuid exercise_id FK
        string ref_id
        enum objective_type "detection/response/deliverable"
        text description
        string validator
        text validator_params
        int points
        bool achieved
        text evidence
        timestamp achieved_at
    }

    AFTER_ACTION_REPORTS {
        uuid id PK
        uuid exercise_id FK UK
        text report_json
        text report_html
        timestamp generated_at
    }

    AUDIT_LOGS {
        uuid id PK
        timestamp timestamp
        uuid user_id FK
        uuid tenant_id FK
        string action
        string resource_type
        string resource_id
        text detail
    }
```

### State Machines

**Range States:**
```
created --> provisioning --> ready --> running --> stopped --> destroying --> destroyed
                  |                      |           |                         ^
                  v                      |           |                         |
                failed ------->----------+-----------+--------> destroying --->+
```

Valid transitions defined in `control-plane/api/app/models.py`:
- `created` -> `provisioning`, `destroyed`
- `provisioning` -> `ready`, `failed`
- `ready` -> `running`, `destroying`
- `running` -> `stopped`, `destroying`
- `stopped` -> `running`, `destroying`
- `destroying` -> `destroyed`, `failed`
- `failed` -> `provisioning`, `destroying`, `destroyed`

**Exercise States:**
```
pending --> running --> paused --> running (resume)
               |          |
               v          v
           completed   cancelled
```

Valid transitions:
- `pending` -> `running`, `cancelled`
- `running` -> `paused`, `completed`, `cancelled`
- `paused` -> `running`, `cancelled`
- `completed` -> (terminal)
- `cancelled` -> (terminal)

---

## Technology Decisions

| Decision | Choice | Rationale | Alternatives Considered |
|----------|--------|-----------|------------------------|
| **API Framework** | FastAPI 0.115+ | Async-ready, auto OpenAPI docs, Pydantic v2, excellent performance | Django REST, Flask |
| **ORM** | SQLAlchemy 2.0 | Mature, async-capable, excellent PostgreSQL support, migration ecosystem | Tortoise ORM, raw SQL |
| **Database** | PostgreSQL 16 | ACID compliance, JSON support, excellent tooling, proven at scale | MySQL, CockroachDB |
| **Task Queue** | Celery + Redis | Battle-tested, Python-native, rich monitoring (Flower), retry logic | Dramatiq, Huey, RQ |
| **Auth** | Keycloak 24 OIDC | Enterprise SSO, MFA, LDAP/AD federation, standard OIDC | Auth0, Okta, custom |
| **Search/Telemetry** | OpenSearch 2.13 | Open-source, rich query DSL, dashboards, community support | Elasticsearch, Loki |
| **Object Storage** | MinIO | S3-compatible, self-hosted, lightweight, erasure coding | Ceph, local filesystem |
| **Frontend** | Angular 17+ | Enterprise-grade, Material M3, TypeScript-first, reactive forms | React, Vue, Svelte |
| **IaC** | Terraform + Packer | Declarative, Proxmox provider, reproducible image builds | Ansible, Pulumi |
| **Virtualization** | Proxmox VE | Open-source, KVM/LXC, API-driven, VLAN support, clustering | VMware, OpenStack |
| **AI Integration** | Multi-backend fleet | Provider flexibility, fallback chains, local option (Ollama) | Single vendor lock-in |
| **Container Dev** | Docker Compose | Simple local development, overlay pattern for optional services | Podman, Kind |
| **WebSocket** | FastAPI native + Redis | Native async support, Redis pub/sub for multi-worker fan-out | Socket.IO, Centrifugo |

---

## Scalability Design

### Capacity Targets

| Metric | Target | Design Strategy |
|--------|--------|----------------|
| Concurrent users | 1,200 | Horizontal API scaling, connection pooling, Redis sessions |
| Total VMs | 70,000 | Batch provisioning, Proxmox cluster federation, queue isolation |
| Telemetry events/sec | 100,000 | OpenSearch sharding, bulk ingest, pipeline routing |
| WebSocket connections | 10,000 | Per-user limits (50), Redis pub/sub, graceful degradation |
| Ranges per tenant | 500 | Composite indexes on (tenant_id, state), query optimization |

### Database Partitioning

- **Tenant isolation**: All queries scoped by `tenant_id` with composite indexes
- **Index strategy**: `ix_ranges_tenant_state`, `ix_exercises_tenant_state`, `ix_audit_tenant_ts`
- **Connection pooling**: PgBouncer in transaction mode for production
- **Read replicas**: Secondary PostgreSQL instances for reporting queries

### Queue Isolation

- **Default queue**: API-triggered tasks (CRUD, small operations)
- **Provision queue**: Range provisioning tasks (long-running, resource-intensive)
- **Scenario queue**: Scenario execution tasks (I/O-bound, telemetry generation)
- **AI queue**: LLM inference requests (variable latency, rate-limited)

### Horizontal Scaling

```mermaid
graph TB
    LB["Load Balancer<br/>nginx / HAProxy"]
    
    subgraph "API Pool (N instances)"
        API1["API Worker 1"]
        API2["API Worker 2"]
        API3["API Worker N"]
    end

    subgraph "Celery Pool (M workers)"
        W1["Worker 1<br/>default + provision"]
        W2["Worker 2<br/>scenario"]
        W3["Worker M<br/>ai"]
    end

    subgraph "Data Tier"
        PG_PRIMARY["PostgreSQL Primary"]
        PG_REPLICA["PostgreSQL Replica"]
        REDIS_CLUSTER["Redis Sentinel"]
        OS_CLUSTER["OpenSearch Cluster<br/>3+ nodes"]
    end

    LB --> API1
    LB --> API2
    LB --> API3
    API1 --> PG_PRIMARY
    API2 --> PG_PRIMARY
    API3 --> PG_PRIMARY
    PG_PRIMARY --> PG_REPLICA
    API1 --> REDIS_CLUSTER
    W1 --> REDIS_CLUSTER
    W1 --> PG_PRIMARY
    W2 --> PG_PRIMARY
    W2 --> OS_CLUSTER
```

---

## Multi-Tenancy Model

TrueNorth Range enforces tenant isolation at every layer of the stack:

| Layer | Isolation Mechanism |
|-------|-------------------|
| **Authentication** | Keycloak realm per tenant (or shared realm with `tenant_id` claim) |
| **API** | All queries filtered by `user.tenant_id`; admin role bypasses |
| **Database** | `tenant_id` FK on all core tables; composite indexes for performance |
| **Object Storage** | MinIO bucket per tenant: `tn-{tenant_slug}-artifacts` |
| **Telemetry** | OpenSearch index per range: `tn-range-{range_uuid}-*` |
| **Network** | VLAN segmentation per range in Proxmox |
| **RBAC** | Fine-grained permissions mapped to 5 roles per tenant |
| **Audit** | All actions logged with `tenant_id` and `user_id` |

### Tenant Access Control Flow

```mermaid
sequenceDiagram
    participant U as User
    participant API as API Middleware
    participant RBAC as RBAC Engine
    participant DB as Database

    U->>API: Request with JWT
    API->>API: Validate JWT, extract tenant_id + role
    API->>RBAC: Check permissions for role
    RBAC-->>API: Allowed / Denied
    API->>DB: Query with WHERE tenant_id = ?
    DB-->>API: Tenant-scoped results
    API-->>U: Response
```

---

## Network Architecture

### Range Network Isolation

Each provisioned range receives dedicated network segmentation:

```mermaid
graph TB
    subgraph "Physical Network"
        CORE_SW["Core Switch<br/>Trunk ports"]
    end

    subgraph "Proxmox Cluster"
        subgraph "Range A (VLAN 100-109)"
            A_DC["Domain Controller"]
            A_WS["Workstations (5)"]
            A_SRV["File Server"]
            A_FW["pfSense Firewall"]
        end

        subgraph "Range B (VLAN 200-209)"
            B_DC["Domain Controller"]
            B_WS["Workstations (10)"]
            B_SRV["Web Server"]
            B_FW["pfSense Firewall"]
        end

        subgraph "Management (VLAN 1)"
            MGMT["Platform Services<br/>API, DB, Redis"]
        end
    end

    CORE_SW --> A_FW
    CORE_SW --> B_FW
    CORE_SW --> MGMT
    A_FW --> A_DC
    A_FW --> A_WS
    A_FW --> A_SRV
    B_FW --> B_DC
    B_FW --> B_WS
    B_FW --> B_SRV
```

### Firewall Rules

| Rule | Source | Destination | Action | Purpose |
|------|--------|-------------|--------|---------|
| Management access | Platform VLAN | Range VLAN | Allow (SSH, WinRM) | Provisioning and telemetry |
| Range isolation | Range VLAN A | Range VLAN B | Deny | Prevent cross-range access |
| Internet egress | Range VLAN | Internet | Configurable | Controlled internet access |
| Sensor reporting | Range VLAN | OpenSearch | Allow (9200) | Telemetry shipping |
| DNS resolution | Range VLAN | DNS Server | Allow (53) | Name resolution |

---

## AI Architecture

### Fleet Routing

The AI Orchestrator supports multiple LLM backends with intelligent routing:

```mermaid
graph TB
    REQ["Incoming AI Request"]
    FLEET["Fleet Router<br/>fleet.py"]

    subgraph "Backend Pool"
        OAI["OpenAI<br/>gpt-4o, gpt-4o-mini"]
        ANT["Anthropic<br/>claude-sonnet, claude-haiku"]
        OLL["Ollama Fleet<br/>OLLAMA_NODES env"]
        MOCK["Mock Backend<br/>Static responses"]
    end

    subgraph "Selection Strategy"
        TAG["Tag-based Selection<br/>capability tags"]
        FB["Fallback Chain<br/>primary -> secondary -> mock"]
        LB_AI["Load Balancing<br/>Round-robin across nodes"]
    end

    REQ --> FLEET
    FLEET --> TAG
    FLEET --> FB
    FLEET --> LB_AI
    TAG --> OAI
    TAG --> ANT
    TAG --> OLL
    FB --> MOCK
```

### AI Capabilities

| Capability | Endpoint | Input | Output | Use Case |
|-----------|----------|-------|--------|----------|
| **Detection Rule Gen** | `POST /ai/detection-rule` | Threat description | Sigma-style YAML rule | Automated detection engineering |
| **Scenario Suggest** | `POST /ai/scenario-suggest` | Existing scenario YAML | Modified/enhanced scenario | Scenario variation and difficulty tuning |
| **AAR Analysis** | `POST /ai/aar-analysis` | Exercise data + telemetry | Narrative analysis + recommendations | Automated after-action insights |
| **General Generate** | `POST /ai/generate` | Free-form prompt | Generated text | Flexible AI assistance |

### Ollama Fleet Configuration

```bash
# Environment variable for multi-node Ollama fleet
OLLAMA_NODES=http://gpu-node-1:11434,http://gpu-node-2:11434,http://gpu-node-3:11434

# Tag-based model selection
OLLAMA_DETECTION_MODEL=codellama:7b
OLLAMA_SCENARIO_MODEL=llama3:70b
OLLAMA_AAR_MODEL=mixtral:8x7b
```

---

## Security Architecture

For detailed security documentation, see [security.md](security.md).

### Authentication Flow

```mermaid
sequenceDiagram
    participant U as User Browser
    participant WEB as Angular App
    participant KC as Keycloak
    participant API as Control Plane API

    U->>WEB: Navigate to app
    WEB->>KC: Redirect to OIDC login
    U->>KC: Enter credentials
    KC->>KC: Validate + MFA
    KC-->>WEB: Authorization code
    WEB->>KC: Exchange code for tokens
    KC-->>WEB: Access token (JWT) + Refresh token
    WEB->>API: Request with Bearer token
    API->>API: Validate JWT signature + claims
    API->>API: Extract role, tenant_id
    API-->>WEB: Authorized response
```

### RBAC Summary

| Role | Key Permissions |
|------|----------------|
| **admin** | All permissions (full platform access) |
| **instructor** | Range lifecycle, scenario CRUD, exercise management, AAR generation |
| **range_ops** | Infrastructure-focused: range CRUD, provisioning, templates |
| **student** | Read ranges/templates/scenarios, start/complete exercises, view AARs |
| **observer** | Read-only access to all resources plus telemetry |

See `control-plane/api/app/rbac.py` for the complete 25-permission matrix.