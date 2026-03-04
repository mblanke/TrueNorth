# TrueNorth Range — Deployment Guide

> Production deployment guide covering Docker Compose, Kubernetes Helm, SSL/TLS, database, monitoring, backup, and troubleshooting.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Hardware Requirements](#hardware-requirements)
- [Docker Compose Deployment](#docker-compose-deployment)
- [Kubernetes Helm Deployment](#kubernetes-helm-deployment)
- [SSL/TLS Configuration](#ssltls-configuration)
- [Database Setup](#database-setup)
- [Redis Configuration](#redis-configuration)
- [MinIO Setup](#minio-setup)
- [OpenSearch Cluster Setup](#opensearch-cluster-setup)
- [Keycloak Realm Configuration](#keycloak-realm-configuration)
- [AI Orchestrator Fleet Setup](#ai-orchestrator-fleet-setup)
- [Monitoring Stack](#monitoring-stack)
- [Backup Configuration](#backup-configuration)
- [Health Check Endpoints](#health-check-endpoints)
- [Environment Variables Reference](#environment-variables-reference)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Docker Engine** 24+ and **Docker Compose** v2.24+
- **Python** 3.11+ (for CLI tools and local development)
- **Node.js** 20+ (for Angular frontend builds)
- **Terraform** 1.7+ (for Proxmox infrastructure provisioning)
- **Packer** 1.10+ (for VM template building)
- **(Optional)** Proxmox VE 8.x cluster with API access
- **(Optional)** GPU-equipped nodes for local Ollama LLM inference
- **(Optional)** Kubernetes 1.28+ cluster with Helm 3.12+

---

## Hardware Requirements

### Development (10 users)

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 4 cores | 8 cores |
| RAM | 16 GB | 32 GB |
| Disk | 100 GB SSD | 250 GB NVMe |
| Network | 1 Gbps | 1 Gbps |

### Staging (100 users)

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 16 cores | 32 cores |
| RAM | 64 GB | 128 GB |
| Disk | 500 GB NVMe | 1 TB NVMe |
| Network | 10 Gbps | 10 Gbps |
| Proxmox Nodes | 2 | 3 |

### Production (1,000+ users)

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| API Servers | 3 instances, 8 cores / 32 GB each | 5 instances, 16 cores / 64 GB each |
| Database | Primary + 1 replica, 16 cores / 64 GB | Primary + 2 replicas, 32 cores / 128 GB |
| Redis | 3-node Sentinel, 8 GB each | 3-node Sentinel, 16 GB each |
| OpenSearch | 3-node cluster, 32 GB each | 5-node cluster, 64 GB each |
| Celery Workers | 5 workers, 8 cores / 16 GB each | 10 workers, 16 cores / 32 GB each |
| Proxmox Cluster | 5+ nodes, 64 cores / 256 GB each | 10+ nodes, 128 cores / 512 GB each |
| Storage | 10 TB distributed | 50 TB distributed |
| Network | 25 Gbps spine/leaf | 100 Gbps spine/leaf |

---

## Docker Compose Deployment

### Development (Quick Start)

```bash
cd infra/platform/docker

# Core services only
docker compose -f compose.dev.yml up -d

# Verify all services are running
docker compose -f compose.dev.yml ps

# Check API health
curl http://localhost:8080/health
```

**Services launched:**

| Service | Container | Port | URL |
|---------|-----------|------|-----|
| Control Plane API | tn-api | 8080 | http://localhost:8080 |
| Celery Worker | tn-worker | -- | -- |
| PostgreSQL | tn-postgres | 5432 | -- |
| Redis | tn-redis | 6379 | -- |
| OpenSearch | tn-opensearch | 9200 | http://localhost:9200 |
| OpenSearch Dashboards | tn-dashboards | 5602 | http://localhost:5602 |
| Keycloak | tn-keycloak | 8180 | http://localhost:8180 |
| MinIO | tn-minio | 9000/9001 | http://localhost:9001 |
| AI Orchestrator | tn-ai | 6000 | http://localhost:6000 |

### With HELK (Threat Hunting) Overlay

```bash
docker compose -f compose.dev.yml \
               -f compose.helk.yml up -d
```

Adds: Elasticsearch, Logstash, Kibana (5601), Kafka, Spark/Jupyter.

### With Velociraptor Overlay

```bash
docker compose -f compose.dev.yml \
               -f compose.velociraptor.yml up -d
```

Adds: Velociraptor server with agent management.

### Full Stack (All Overlays)

```bash
docker compose -f compose.dev.yml \
               -f compose.helk.yml \
               -f compose.velociraptor.yml up -d
```

> **Warning:** Full stack requires 32+ GB RAM minimum.

### Staging with Docker Compose

For staging deployments, create a `compose.staging.yml` override:

```yaml
# compose.staging.yml
services:
  api:
    environment:
      AUTH_DISABLED: "false"
      KEYCLOAK_URL: "https://keycloak.staging.truenorth.local"
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "4"
          memory: 8G

  worker:
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: "4"
          memory: 8G

  postgres:
    volumes:
      - pgdata_staging:/var/lib/postgresql/data
    environment:
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"

volumes:
  pgdata_staging:
    driver: local
```

```bash
docker compose -f compose.dev.yml \
               -f compose.staging.yml up -d
```

---

## Kubernetes Helm Deployment

### Prerequisites

- Kubernetes 1.28+ cluster
- Helm 3.12+
- Persistent storage class (for PostgreSQL, MinIO, OpenSearch)
- Ingress controller (nginx or Traefik)
- cert-manager (for automatic TLS)

### Install via Helm

```bash
# Add the TrueNorth Helm repository
helm repo add truenorth https://charts.truenorth.example.com
helm repo update

# Install with default values
helm install truenorth truenorth/truenorth-range \
  --namespace truenorth \
  --create-namespace \
  --values values-production.yaml

# Or install from local chart
helm install truenorth ./infra/helm/truenorth-range \
  --namespace truenorth \
  --create-namespace \
  --values values-production.yaml
```

### Example `values-production.yaml`

```yaml
global:
  domain: truenorth.example.com
  storageClass: fast-ssd
  imagePullSecrets:
    - name: registry-secret

api:
  replicas: 3
  resources:
    requests:
      cpu: "2"
      memory: 4Gi
    limits:
      cpu: "4"
      memory: 8Gi
  env:
    AUTH_DISABLED: "false"
    KEYCLOAK_URL: "https://keycloak.truenorth.example.com"
    KEYCLOAK_REALM: "truenorth"

worker:
  replicas: 5
  queues:
    - name: default
      replicas: 2
    - name: provision
      replicas: 2
    - name: scenario
      replicas: 1
  resources:
    requests:
      cpu: "2"
      memory: 4Gi

postgresql:
  enabled: true
  architecture: replication
  primary:
    resources:
      requests:
        cpu: "4"
        memory: 16Gi
  readReplicas:
    replicaCount: 2
  auth:
    existingSecret: postgres-credentials

redis:
  enabled: true
  architecture: sentinel
  sentinel:
    enabled: true

opensearch:
  enabled: true
  replicas: 3
  resources:
    requests:
      cpu: "4"
      memory: 32Gi
  persistence:
    size: 500Gi

minio:
  enabled: true
  replicas: 4
  persistence:
    size: 200Gi

keycloak:
  enabled: true
  replicas: 2
  ingress:
    enabled: true
    hostname: keycloak.truenorth.example.com

aiOrchestrator:
  enabled: true
  replicas: 2
  env:
    AI_BACKEND: "openai"
    OPENAI_API_KEY:
      valueFrom:
        secretKeyRef:
          name: ai-secrets
          key: openai-key

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: truenorth.example.com
      paths:
        - path: /
          service: api
  tls:
    - secretName: truenorth-tls
      hosts:
        - truenorth.example.com

monitoring:
  enabled: true
  prometheus:
    enabled: true
  grafana:
    enabled: true
```

### Upgrade

```bash
helm upgrade truenorth truenorth/truenorth-range \
  --namespace truenorth \
  --values values-production.yaml
```

---

## SSL/TLS Configuration

### With cert-manager (Kubernetes)

```yaml
# cluster-issuer.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@truenorth.example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
```

### Manual TLS (Docker Compose)

```bash
# Generate self-signed certs (development only)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/truenorth.key \
  -out certs/truenorth.crt \
  -subj "/CN=truenorth.local"

# Or use Let's Encrypt with certbot
certbot certonly --standalone -d truenorth.example.com
```

Configure nginx reverse proxy:

```nginx
server {
    listen 443 ssl http2;
    server_name truenorth.example.com;

    ssl_certificate     /etc/nginx/certs/truenorth.crt;
    ssl_certificate_key /etc/nginx/certs/truenorth.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://api:8080/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## Database Setup

### PostgreSQL with PgBouncer (Production)

**PostgreSQL 16 Configuration (`postgresql.conf`):**

```ini
# Connection settings
max_connections = 200
listen_addresses = '*'

# Memory
shared_buffers = 4GB               # 25% of available RAM
effective_cache_size = 12GB         # 75% of available RAM
work_mem = 64MB
maintenance_work_mem = 512MB

# WAL
wal_level = replica
max_wal_senders = 5
wal_keep_size = 2GB

# Performance
random_page_cost = 1.1              # SSD storage
effective_io_concurrency = 200
```

**PgBouncer Configuration (`pgbouncer.ini`):**

```ini
[databases]
truenorth_range = host=postgres port=5432 dbname=truenorth_range

[pgbouncer]
listen_port = 6432
listen_addr = 0.0.0.0
auth_type = scram-sha-256
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 25
min_pool_size = 5
reserve_pool_size = 5
server_idle_timeout = 300
```

### Database Migrations (Alembic)

```bash
# Initialize migrations (first time)
cd control-plane/api
alembic init alembic

# Generate migration from model changes
alembic revision --autogenerate -m "Add new column"

# Apply migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

---

## Redis Configuration

### Production Redis Sentinel

```yaml
# docker-compose Redis Sentinel setup
services:
  redis-master:
    image: redis:7-alpine
    command: redis-server --maxmemory 2gb --maxmemory-policy allkeys-lru
    volumes:
      - redis-data:/data

  redis-sentinel:
    image: redis:7-alpine
    command: redis-sentinel /etc/redis/sentinel.conf
    volumes:
      - ./redis-sentinel.conf:/etc/redis/sentinel.conf
```

**sentinel.conf:**
```
sentinel monitor truenorth redis-master 6379 2
sentinel down-after-milliseconds truenorth 5000
sentinel failover-timeout truenorth 60000
sentinel parallel-syncs truenorth 1
```

---

## MinIO Setup

### Production Multi-Node Configuration

```bash
# Create MinIO cluster (4 nodes, 4 drives each)
minio server http://minio{1...4}/data{1...4} \
  --console-address ":9001"

# Create required buckets
mc alias set truenorth http://minio:9000 minioadmin minioadmin
mc mb truenorth/truenorth-artifacts
mc mb truenorth/truenorth-reports
mc mb truenorth/truenorth-templates
mc mb truenorth/truenorth-packer-images
```

### Bucket Policies

| Bucket | Policy | Purpose |
|--------|--------|---------|
| `truenorth-artifacts` | Private | Exercise artifacts, evidence uploads |
| `truenorth-reports` | Private | AAR HTML/PDF reports |
| `truenorth-templates` | Read-only for users | Packer and Terraform templates |
| `truenorth-packer-images` | Private | VM disk images |

---

## OpenSearch Cluster Setup

### Production 3-Node Cluster

```yaml
# docker-compose OpenSearch cluster
services:
  opensearch-node1:
    image: opensearchproject/opensearch:2.13.0
    environment:
      cluster.name: truenorth-cluster
      node.name: opensearch-node1
      discovery.seed_hosts: opensearch-node1,opensearch-node2,opensearch-node3
      cluster.initial_cluster_manager_nodes: opensearch-node1,opensearch-node2,opensearch-node3
      OPENSEARCH_JAVA_OPTS: "-Xms16g -Xmx16g"
    volumes:
      - opensearch-data1:/usr/share/opensearch/data
    ulimits:
      memlock:
        soft: -1
        hard: -1
```

### Index Template

```json
{
  "index_patterns": ["tn-range-*"],
  "template": {
    "settings": {
      "number_of_shards": 3,
      "number_of_replicas": 1,
      "refresh_interval": "5s"
    },
    "mappings": {
      "properties": {
        "timestamp": { "type": "date" },
        "tenant_id": { "type": "keyword" },
        "range_id": { "type": "keyword" },
        "event.action": { "type": "keyword" },
        "process.name": { "type": "keyword" },
        "process.command_line": { "type": "text" },
        "host.name": { "type": "keyword" },
        "source": { "type": "keyword" }
      }
    }
  }
}
```

---

## Keycloak Realm Configuration

### Step-by-Step Setup

1. **Access Admin Console:** Navigate to `http://localhost:8180/admin/`
2. **Create Realm:** Click "Create Realm" and name it `truenorth`
3. **Create API Client:**
   - Client ID: `truenorth-api`
   - Client type: Confidential
   - Valid redirect URIs: `http://localhost:8080/*`
   - Client authentication: On
4. **Create Web Client:**
   - Client ID: `truenorth-web`
   - Client type: Public
   - Valid redirect URIs: `http://localhost:4200/*`
   - Web Origins: `http://localhost:4200`
5. **Create Roles:** `admin`, `instructor`, `student`, `observer`, `range_ops`
6. **Add Custom Claim:**
   - Create a Client Scope named `tenant_id`
   - Add a User Attribute Mapper for `tenant_id`
   - Assign scope to clients
7. **Create Users and Assign Roles**

### Keycloak JSON Export

```json
{
  "realm": "truenorth",
  "enabled": true,
  "sslRequired": "external",
  "roles": {
    "realm": [
      { "name": "admin" },
      { "name": "instructor" },
      { "name": "student" },
      { "name": "observer" },
      { "name": "range_ops" }
    ]
  },
  "clients": [
    {
      "clientId": "truenorth-api",
      "enabled": true,
      "clientAuthenticatorType": "client-secret",
      "redirectUris": ["*"],
      "webOrigins": ["*"]
    },
    {
      "clientId": "truenorth-web",
      "enabled": true,
      "publicClient": true,
      "redirectUris": ["*"],
      "webOrigins": ["*"]
    }
  ]
}
```

---

## AI Orchestrator Fleet Setup

### Single Backend (OpenAI)

```bash
AI_BACKEND=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

### Multi-Backend (Fallback Chain)

```bash
AI_BACKEND=fleet
AI_PRIMARY_BACKEND=openai
AI_FALLBACK_BACKEND=anthropic
AI_FINAL_FALLBACK=mock

OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

### Ollama Fleet (Self-Hosted LLMs)

```bash
AI_BACKEND=ollama
OLLAMA_NODES=http://gpu-node-1:11434,http://gpu-node-2:11434,http://gpu-node-3:11434

# Tag-based model selection per capability
OLLAMA_DETECTION_MODEL=codellama:7b
OLLAMA_SCENARIO_MODEL=llama3:70b
OLLAMA_AAR_MODEL=mixtral:8x7b
OLLAMA_DEFAULT_MODEL=llama3:8b
```

**GPU Node Requirements:**

| Model Size | GPU VRAM | Recommended GPU |
|-----------|----------|-----------------|
| 7B params | 8 GB | RTX 3070 / A10 |
| 13B params | 16 GB | RTX 4090 / A16 |
| 70B params | 40 GB | A100 40GB |
| 8x7B (MoE) | 48 GB | A100 80GB / 2xA6000 |

---

## Monitoring Stack

### Deployment

```bash
# Deploy monitoring stack
docker compose -f compose.dev.yml -f compose.monitoring.yml up -d
```

### OpenSearch Dashboards

- **URL:** http://localhost:5602
- **Purpose:** Telemetry visualization, range event exploration
- **Pre-built dashboards:**
  - Range Telemetry Overview
  - Exercise Event Timeline
  - Detection Rule Hits
  - Scenario Injection Map

### Celery Flower (Worker Monitoring)

```bash
pip install flower
celery -A worker.celery_app flower --port=5555
```

- **URL:** http://localhost:5555
- **Monitors:** Task queue depth, worker utilization, task failure rates

### Application Metrics

The API exposes Prometheus-compatible metrics at `/metrics`:

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | Counter | Total HTTP requests by method and status |
| `http_request_duration_seconds` | Histogram | Request latency |
| `websocket_connections_active` | Gauge | Active WebSocket connections |
| `celery_tasks_total` | Counter | Total Celery tasks by name and state |
| `ranges_by_state` | Gauge | Current ranges per state |
| `exercises_active` | Gauge | Active exercise count |

---

## Backup Configuration

### Automated Backup Schedule

| Component | Frequency | Retention | Method |
|-----------|-----------|-----------|--------|
| PostgreSQL | Every 6 hours | 30 days | pg_dump + S3 upload |
| MinIO | Daily | 90 days | mc mirror to remote |
| OpenSearch | Daily snapshots | 14 days | Snapshot repository |
| Keycloak | Daily export | 30 days | Realm JSON export |
| Configuration | On change | Indefinite | Git repository |

### PostgreSQL Backup Script

```bash
#!/bin/bash
# /scripts/backup-postgres.sh
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="truenorth_backup_${TIMESTAMP}.sql.gz"

# Dump and compress
docker exec tn-postgres pg_dump -U truenorth truenorth_range | gzip > "/backups/${BACKUP_FILE}"

# Upload to MinIO
mc cp "/backups/${BACKUP_FILE}" minio/truenorth-backups/postgres/

# Cleanup local files older than 7 days
find /backups -name "truenorth_backup_*.sql.gz" -mtime +7 -delete

echo "Backup completed: ${BACKUP_FILE}"
```

### Restore Procedures

```bash
# PostgreSQL restore
gunzip < truenorth_backup_20260115.sql.gz | docker exec -i tn-postgres psql -U truenorth truenorth_range

# MinIO restore
mc mirror minio-backup/truenorth-artifacts /restore/artifacts/

# OpenSearch snapshot restore
curl -X POST "http://opensearch:9200/_snapshot/backup/snapshot_20260115/_restore"
```

---

## Health Check Endpoints

| Endpoint | Port | Expected Response |
|----------|------|-------------------|
| `GET /health` | 8080 | `{"status": "ok", "version": "1.0.0", "db": true}` |
| `GET /` (OpenSearch) | 9200 | `{"cluster_name": "truenorth-cluster"}` |
| `GET /health/ready` (Keycloak) | 8180 | `{"status": "UP"}` |
| `GET /minio/health/live` | 9000 | HTTP 200 |
| Redis `PING` | 6379 | `PONG` |

### Docker Compose Healthchecks

```yaml
services:
  api:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  postgres:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U truenorth"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
```

---

## Environment Variables Reference

### Control Plane API

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://truenorth:truenorth@postgres:5432/truenorth_range` | Yes | PostgreSQL connection string |
| `REDIS_URL` | `redis://redis:6379/0` | Yes | Redis connection URL |
| `AUTH_DISABLED` | `true` | No | Disable auth for development |
| `KEYCLOAK_URL` | `http://keycloak:8080` | Prod | Keycloak base URL |
| `KEYCLOAK_REALM` | `truenorth` | Prod | OIDC realm name |
| `KEYCLOAK_CLIENT_ID` | `truenorth-api` | Prod | OIDC client ID |
| `OPENSEARCH_URL` | `http://opensearch:9200` | Yes | OpenSearch connection URL |
| `MINIO_ENDPOINT` | `minio:9000` | Yes | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | Yes | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | Yes | MinIO secret key |
| `CORS_ORIGINS` | `*` | Prod | Allowed CORS origins |
| `LOG_LEVEL` | `INFO` | No | Logging level |

### Celery Worker

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `PROVISIONER_BACKEND` | `mock` | No | `mock` or `terraform` |
| `PROXMOX_API_URL` | -- | Terraform | Proxmox API endpoint |
| `PROXMOX_TOKEN_ID` | -- | Terraform | Proxmox API token ID |
| `PROXMOX_TOKEN_SECRET` | -- | Terraform | Proxmox API secret |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Yes | Celery broker URL |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Yes | Result backend URL |

### AI Orchestrator

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `AI_BACKEND` | `mock` | No | Backend type: `openai`, `anthropic`, `ollama`, `fleet`, `mock` |
| `OPENAI_API_KEY` | -- | If OpenAI | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | No | Default OpenAI model |
| `ANTHROPIC_API_KEY` | -- | If Anthropic | Anthropic API key |
| `OLLAMA_NODES` | -- | If Ollama | Comma-separated Ollama node URLs |
| `AI_REQUEST_TIMEOUT` | `60` | No | LLM request timeout (seconds) |

---

## Troubleshooting

### Common Issues

| Symptom | Cause | Solution |
|---------|-------|----------|
| API returns 500 on startup | Database not ready | Wait for PostgreSQL healthcheck, check `DATABASE_URL` |
| Celery tasks stuck in pending | Redis not reachable | Check Redis connectivity, verify `CELERY_BROKER_URL` |
| OpenSearch returns 503 | Cluster not formed | Check node discovery settings, verify JVM heap |
| Keycloak login fails | CORS configuration | Add frontend URL to Keycloak client web origins |
| WebSocket disconnects | Reverse proxy timeout | Set `proxy_read_timeout 3600s` in nginx |
| Range provisioning fails | Proxmox API unreachable | Check `PROXMOX_API_URL`, verify API token permissions |
| AI endpoints timeout | LLM backend slow | Increase `AI_REQUEST_TIMEOUT`, check model availability |
| MinIO upload fails | Bucket doesn't exist | Run `mc mb truenorth/truenorth-artifacts` |
| Database migration fails | Schema drift | Run `alembic stamp head`, then `alembic upgrade head` |
| Permission denied (403) | Wrong role | Check user role in Keycloak, verify RBAC mapping |

### Diagnostic Commands

```bash
# Check all container statuses
docker compose -f compose.dev.yml ps

# View API logs
docker compose -f compose.dev.yml logs -f api

# View worker logs
docker compose -f compose.dev.yml logs -f worker

# Check PostgreSQL connectivity
docker exec tn-postgres pg_isready -U truenorth

# Check Redis connectivity
docker exec tn-redis redis-cli ping

# Check OpenSearch cluster health
curl -s http://localhost:9200/_cluster/health | python -m json.tool

# Check Celery worker status
celery -A worker.celery_app inspect active

# Test Keycloak token endpoint
curl -X POST "http://localhost:8180/realms/truenorth/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=truenorth-api&username=admin&password=admin"
```