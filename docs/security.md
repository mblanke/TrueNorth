# TrueNorth Range — Security Guide

> Authentication, authorization, network security, encryption, audit logging, vulnerability management, and compliance mapping for TrueNorth Range deployments.

---

## Table of Contents

- [Security Architecture Overview](#security-architecture-overview)
- [Authentication: Keycloak OIDC](#authentication-keycloak-oidc)
- [Authorization: RBAC Model](#authorization-rbac-model)
- [Permissions Matrix](#permissions-matrix)
- [Network Security](#network-security)
- [Encryption](#encryption)
- [Rate Limiting and Input Validation](#rate-limiting-and-input-validation)
- [Audit Logging](#audit-logging)
- [Vulnerability Management](#vulnerability-management)
- [Security Hardening Checklist](#security-hardening-checklist)
- [Compliance Mapping](#compliance-mapping)

---

## Security Architecture Overview

```mermaid
flowchart TB
    subgraph External
        USER[Browser / CLI]
    end

    subgraph DMZ
        LB[Load Balancer<br>TLS Termination]
        WAF[Web Application Firewall]
    end

    subgraph Application Zone
        API[Control Plane API<br>JWT Validation]
        AI[AI Orchestrator]
    end

    subgraph Auth Zone
        KC[Keycloak<br>OIDC Provider]
    end

    subgraph Data Zone
        PG[(PostgreSQL)]
        RS[(Redis<br>Auth Required)]
        OS[(OpenSearch<br>TLS + Auth)]
        MIO[(MinIO<br>TLS + IAM)]
    end

    subgraph Range Zone
        VMS[Range VMs<br>VLAN Isolated]
    end

    USER -->|HTTPS| LB
    LB -->|TLS| WAF
    WAF -->|Forward| API
    USER -->|OIDC Flow| KC
    KC -->|JWT| USER
    API -->|Validate JWT| KC
    API -->|Encrypted| PG
    API -->|Auth| RS
    API -->|TLS| OS
    API -->|TLS| MIO
    API -.->|Provision| VMS

    style DMZ fill:#fff3cd
    style Auth Zone fill:#d4edda
    style Data Zone fill:#cce5ff
    style Range Zone fill:#f8d7da
```

### Security Principles

1. **Defense in Depth** — Multiple security layers from network to application
2. **Least Privilege** — Each component and user gets minimum required permissions
3. **Zero Trust** — Every request is authenticated and authorized, no implicit trust
4. **Encryption** — TLS at the edge and to OpenSearch; stored credentials sealed by the
   application. Not everywhere: see [Encryption](#encryption) for the gaps
5. **Audit** — State changes in most sections write an audit row; see
   [Audit Logging](#audit-logging) for what is and is not covered
6. **Tenant Isolation** — Strict data separation between tenants

---

## Authentication: Keycloak OIDC

### Architecture

TrueNorth Range uses Keycloak 24 as the OpenID Connect (OIDC) identity provider. All authentication flows go through Keycloak — the API never handles passwords directly.

```mermaid
sequenceDiagram
    participant User
    participant SPA as Angular SPA
    participant KC as Keycloak
    participant API as Control Plane API

    User->>SPA: Navigate to app
    SPA->>KC: Redirect to /auth (PKCE)
    KC->>User: Login page
    User->>KC: Credentials
    KC->>SPA: Authorization code
    SPA->>KC: Exchange code for tokens
    KC->>SPA: Access token + Refresh token
    SPA->>API: API request + Bearer token
    API->>API: Validate JWT signature
    API->>API: Check claims (sub, roles, tenant_id)
    API->>SPA: Response
Note over SPA,KC: Refresh token used to renew access token
```

### JWT Token Structure

```json
{
  "iss": "https://auth.truenorth.local/realms/truenorth",
  "sub": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "aud": "truenorth-api",
  "exp": 1705312800,
  "iat": 1705309200,
  "realm_access": {
    "roles": ["instructor"]
  },
  "resource_access": {
    "truenorth-api": {
      "roles": ["instructor"]
    }
  },
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "preferred_username": "john.doe",
  "email": "john.doe@example.com",
  "name": "John Doe"
}
```

### Keycloak Realm Configuration

Excerpt of `infra/keycloak/realm-truenorth.json` (the maintained source; the installer's
`60-keycloak` enforces the token, session, password and client settings on every run,
and `tests/contracts/test_keycloak_hardening.py` keeps the two equal):

```json
{
  "realm": "truenorth",
  "enabled": true,
  "sslRequired": "external",
  "bruteForceProtected": true,
  "permanentLockout": false,
  "failureFactor": 5,
  "maxFailureWaitSeconds": 300,
  "minimumQuickLoginWaitSeconds": 60,
  "waitIncrementSeconds": 60,
  "maxDeltaTimeSeconds": 3600,
  "passwordPolicy": "length(12) and notUsername(undefined) and passwordHistory(5)",
  "accessTokenLifespan": 300,
  "ssoSessionIdleTimeout": 1800,
  "ssoSessionMaxLifespan": 36000,
  "offlineSessionIdleTimeout": 604800,
  "clients": [
    {
      "clientId": "truenorth-web",
      "publicClient": true,
      "standardFlowEnabled": true,
      "directAccessGrantsEnabled": false,
      "attributes": { "pkce.code.challenge.method": "S256" }
    },
    {
      "clientId": "truenorth-api",
      "publicClient": false,
      "standardFlowEnabled": false,
      "directAccessGrantsEnabled": false,
      "serviceAccountsEnabled": true
    },
    {
      "clientId": "truenorth-cli",
      "publicClient": true,
      "standardFlowEnabled": false,
      "directAccessGrantsEnabled": false,
      "attributes": {
        "oauth2.device.authorization.grant.enabled": "true",
        "pkce.code.challenge.method": "S256"
      }
    },
    {
      "clientId": "truenorth-smoke",
      "enabled": false,
      "publicClient": false,
      "directAccessGrantsEnabled": true
    }
  ]
}
```

`truenorth-smoke` exists only for the installer's smoke test, which enables it for one
token request and disables it again (`install/roles/tn_smoke`). Sign-in flows:
`docs/api.md` ("Authentication").

### API JWT Validation

The Control Plane API validates every incoming JWT:

1. **Signature verification** — RS256 using Keycloak's JWKS endpoint
2. **Issuer check** — Must match configured Keycloak realm URL
3. **Audience check** — Must include `truenorth-api`
4. **Expiration check** — Token must not be expired
5. **Role extraction** — Roles mapped from `realm_access.roles`
6. **Tenant extraction** — `tenant_id` claim for multi-tenant isolation

```python
# Simplified JWT validation flow (see control-plane/api/app/rbac.py)
async def get_current_user(token: str = Depends(oauth2_scheme)):
    payload = jwt.decode(
        token,
        key=jwks_client.get_signing_key(token).key,
        algorithms=["RS256"],
        audience="truenorth-api",
        issuer=settings.KEYCLOAK_ISSUER,
    )
    user = await get_user_by_sub(payload["sub"])
    if not user:
        raise HTTPException(401, "User not found")
    return user
```

---

## Authorization: RBAC Model

TrueNorth Range implements fine-grained Role-Based Access Control defined in `control-plane/api/app/rbac.py`.

### Roles

| Role | Description | Typical User |
|------|-------------|-------------|
| `admin` | Full platform administration | Platform administrators |
| `instructor` | Create/manage ranges, exercises, scenarios | Training instructors |
| `student` | Participate in exercises, view own data | Trainees |
| `observer` | Read-only access to ranges and exercises | Evaluators, auditors |
| `range_ops` | Infrastructure operations, provisioning | DevOps / range operators |

### Permission Categories

Permissions follow the pattern `resource:action`:

| Category | Permissions |
|----------|------------|
| **Range** | `range:create`, `range:read`, `range:update`, `range:delete`, `range:provision`, `range:destroy`, `range:batch_provision`, `range:lease_force_release` |
| **Template** | `template:create`, `template:read`, `template:update`, `template:delete` |
| **Scenario** | `scenario:create`, `scenario:read`, `scenario:update`, `scenario:delete` |
| **Exercise** | `exercise:create`, `exercise:read`, `exercise:update`, `exercise:delete`, `exercise:control` |
| **User** | `user:create`, `user:read`, `user:update`, `user:delete` |
| **Tenant** | `tenant:create`, `tenant:read`, `tenant:update`, `tenant:delete` |
| **Audit** | `audit:read` |
| **Telemetry** | `telemetry:read` |
| **Stats** | `stats:read` |
| **AAR** | `aar:generate`, `aar:read` |
| **Schedule** | `schedule:read`, `schedule:write`, `schedule:admin` (ADR 0004: Students hold none) |

---

## Permissions Matrix

Complete role-to-permission mapping as defined in `rbac.py`:

| Permission | admin | instructor | student | observer | range_ops |
|------------|:-----:|:----------:|:-------:|:--------:|:---------:|
| `range:create` | X | X | | | X |
| `range:read` | X | X | X | X | X |
| `range:update` | X | X | | | X |
| `range:delete` | X | X | | | X |
| `range:provision` | X | X | | | X |
| `range:destroy` | X | X | | | X |
| `range:batch_provision` | X | | | | X |
| `range:lease_force_release` | X | | | | |
| `template:create` | X | X | | | |
| `template:read` | X | X | X | X | X |
| `template:update` | X | X | | | |
| `template:delete` | X | X | | | |
| `scenario:create` | X | X | | | |
| `scenario:read` | X | X | X | X | |
| `scenario:update` | X | X | | | |
| `scenario:delete` | X | X | | | |
| `exercise:create` | X | X | | | |
| `exercise:read` | X | X | X | X | |
| `exercise:update` | X | X | | | |
| `exercise:delete` | X | X | | | |
| `exercise:control` | X | X | | | |
| `user:create` | X | | | | |
| `user:read` | X | X | | | |
| `user:update` | X | | | | |
| `user:delete` | X | | | | |
| `tenant:create` | X | | | | |
| `tenant:read` | X | | | | |
| `tenant:update` | X | | | | |
| `tenant:delete` | X | | | | |
| `audit:read` | X | | | | |
| `telemetry:read` | X | X | X | X | |
| `stats:read` | X | X | | X | X |
| `aar:generate` | X | X | | | |
| `aar:read` | X | X | X | X | |
| `schedule:read` | X | X | | X | X |
| `schedule:write` | X | X | | | |
| `schedule:admin` | X (platform tenant only when `PLATFORM_TENANT_ID` is set) | | | | |

**Platform-shared curriculum.** The QSP qualification spine (`/qsp/import-crosswalk`,
`/qsp/import-competency-crosswalk`) and the competency catalogue (`POST
/competency/frameworks`, `/competency/frameworks/import-nice`) are shared by every tenant.
Writing them needs `course:author` + `tenant:read` (`PLATFORM_CURRICULUM_WRITE`), so only a
platform admin may; an instructor (tenant-level author) is refused with 403.

### Permission Enforcement

Permissions are enforced at the API endpoint level using FastAPI dependency injection:

```python
# Single permission required
@router.post("/ranges", dependencies=[Depends(require_permission(Permission.RANGE_CREATE))])
async def create_range(...):
    ...

# Any of multiple permissions
@router.get("/stats", dependencies=[Depends(require_any_permission(
    Permission.STATS_READ, Permission.RANGE_READ
))])
async def get_stats(...):
    ...

# Tenant-scoped access
@router.get("/ranges/{range_id}")
async def get_range(
    range_id: UUID,
    tenant_check=Depends(require_tenant_access),
    perm_check=Depends(require_permission(Permission.RANGE_READ)),
):
    ...

# Range-specific access
@router.post("/ranges/{range_id}/provision")
async def provision_range(
    range_id: UUID,
    access=Depends(require_range_access),
    perm=Depends(require_permission(Permission.RANGE_PROVISION)),
):
    ...
```

---

## Network Security

### Network Architecture

```mermaid
flowchart TB
    subgraph Public Zone
        INET[Internet]
    end

    subgraph DMZ - VLAN 10
        LB[Load Balancer]
        WAF[WAF]
    end

    subgraph App Zone - VLAN 20
        API[API Servers]
        WRK[Workers]
        AI[AI Orchestrator]
    end

    subgraph Data Zone - VLAN 30
        DB[(PostgreSQL)]
        CACHE[(Redis)]
        SEARCH[(OpenSearch)]
        STORE[(MinIO)]
    end

    subgraph Auth Zone - VLAN 40
        KC[Keycloak]
    end

    subgraph Range Zone - VLAN 100-999
        R1[Range 1 VMs]
        R2[Range 2 VMs]
        RN[Range N VMs]
    end

    INET --> LB
    LB --> WAF
    WAF --> API
    API --> DB
    API --> CACHE
    API --> SEARCH
    API --> STORE
    API --> KC
    WRK --> DB
    WRK --> CACHE
    WRK -.-> R1
    WRK -.-> R2

    style Public Zone fill:#f8d7da
    style DMZ - VLAN 10 fill:#fff3cd
    style App Zone - VLAN 20 fill:#d4edda
    style Data Zone - VLAN 30 fill:#cce5ff
    style Auth Zone - VLAN 40 fill:#d4edda
    style Range Zone - VLAN 100-999 fill:#e2e3e5
```

### VLAN Isolation

| VLAN | Name | Purpose |
|------|------|---------|
| 10 | DMZ | Load balancers, WAF |
| 20 | Application | API servers, workers, AI |
| 30 | Data | PostgreSQL, Redis, OpenSearch, MinIO |
| 40 | Authentication | Keycloak |
| 100-999 | Range VLANs | Individual range environments (isolated per range) |

### Firewall Rules

```
# DMZ -> Application
ALLOW VLAN10 -> VLAN20:8080 (HTTPS to API)
ALLOW VLAN10 -> VLAN20:443  (WebSocket)

# Application -> Data
ALLOW VLAN20 -> VLAN30:5432 (PostgreSQL)
ALLOW VLAN20 -> VLAN30:6379 (Redis)
ALLOW VLAN20 -> VLAN30:9200 (OpenSearch)
ALLOW VLAN20 -> VLAN30:9000 (MinIO)

# Application -> Auth
ALLOW VLAN20 -> VLAN40:8443 (Keycloak)

# Application -> Range (provisioning only)
ALLOW VLAN20 -> VLAN100-999:22   (SSH for provisioning)
ALLOW VLAN20 -> VLAN100-999:5985 (WinRM)

# Range Isolation
DENY  VLAN100 -> VLAN200  (Range-to-range blocked)
DENY  VLAN100 -> VLAN30   (Range-to-data blocked)
ALLOW VLAN100 -> VLAN20:9200 (Telemetry to OpenSearch only)

# Default
DENY ALL
```

### Range VLAN Isolation

Each provisioned range receives a dedicated VLAN (100-999):

- **No cross-range traffic** — Ranges cannot communicate with each other
- **No data zone access** — Ranges cannot directly access PostgreSQL, Redis, etc.
- **Telemetry egress only** — Range VMs can send telemetry to OpenSearch
- **Management ingress only** — Only provisioning workers can reach range VMs

---

## Encryption

### Encryption in Transit

As shipped in `infra/platform/docker/compose.prod.yml` (checked 2026-10-12):

| Connection | Protected | Evidence |
|-----------|----------|----------------|
| Browser → nginx edge | TLS 1.2/1.3, HSTS | `infra/platform/nginx/conf.d/ssl-params.conf`, `snippets/security-headers-common.conf` |
| nginx → API, web, Keycloak | No (plain HTTP on the compose network) | `conf.d/truenorth.conf` |
| API/worker → PostgreSQL (pgbouncer) | No | `DATABASE_URL` with no `sslmode` |
| API/worker → Redis | Password, no TLS | `REDIS_URL=redis://` |
| API → MinIO | No | `MINIO_ENDPOINT=http://minio:9000` |
| API → Keycloak (JWKS) | No | `KEYCLOAK_URL=http://keycloak:8080/auth` |
| API/worker → OpenSearch | TLS, installer's internal CA | security plugin `ssl.http.enabled=true` |
| Kubernetes inter-pod | No service mesh; nothing configures mTLS | `infra/k8s/helm/` |

Internal traffic relies on the host's container network not being reachable from outside.

### TLS Configuration

```nginx
# Recommended TLS settings (nginx; the shipped ssl-params.conf sets the protocols)
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
ssl_prefer_server_ciphers on;
ssl_session_timeout 1d;
ssl_session_cache shared:SSL:50m;
ssl_stapling on;
ssl_stapling_verify on;

# HSTS
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
```

### Encryption at Rest

TrueNorth ships no Vault, no KMS integration, no database TDE and no disk encryption
(LUKS). The one application-level encryption at rest is the sealed-credential columns
below; anything more is the host's job (for example an encrypted volume under
`/var/lib/docker`), and the installer does not set it up.

| Data Store | Encrypted at rest by TrueNorth? | Notes |
|-----------|--------|---------------|
| PostgreSQL | No | Plain data files. Credential columns are sealed (last row) |
| OpenSearch | No | The security plugin provides TLS and auth, not at-rest encryption |
| MinIO | No | No SSE or KMS configured |
| Redis | No | Broker and cache only, not backed up |
| Backups | Partly | Database dumps and MinIO mirrors are plain; the env file and secrets directory are AES-256 encrypted to the escrow RSA public key (`scripts/backup/backup.sh`) |
| Stored credentials (hypervisor passwords and API tokens, AI engine keys) | Yes: Fernet (AES-128-CBC + HMAC-SHA256), sealed by the application | `TN_SECRETS_KEY`, an environment value; no external key manager |

#### Stored credentials

Hypervisor connection passwords and API tokens (`hypervisor_connections.password_encrypted`,
`.api_token`) and AI engine keys (`ai_backend_configs.api_key_encrypted`) are sealed before
they are written and unsealed only where a login needs them (`app/secretbox.py`; the
worker has a byte-identical copy, `worker/secretbox.py`). The API never returns them.

- **Key.** `TN_SECRETS_KEY`: at least 32 random characters (`openssl rand -hex 32`), the
  same value for the api and every worker. The installer generates it once and never
  again (`vault_secrets_key`); Helm takes `secrets.secretsKey`. The dev compose file uses
  a public dev-only value.
- **Without a key** nothing is stored: saving a credential, testing a connection or calling
  an AI engine that has one returns 503 naming the setting. Production compose refuses to
  start without it. A connection or engine with no credential needs no key.
- **Back the key up with the database.** A credential sealed with a lost key cannot be
  read (503); the fix is to re-enter it.
- **Rotation.** Set `TN_SECRETS_KEY=new,old` and restart api and workers: new values are
  sealed with `new`, old ones still open. Re-save each credential, then drop `old`.
- **Upgrade.** Migration `f4b5c6d7e8a9` seals existing plaintext values and refuses to run
  without the key if there are any. Its downgrade unseals them for a rollback.
- **Not covered:** `external_platforms.credentials_encrypted` (LMS integrations). No code
  writes it today; whatever starts to must seal it with the same module.

---

## Rate Limiting and Input Validation

### Rate Limiting

Two layers. The API keeps a per-user-per-address sliding window in Redis
(`control-plane/api/app/middleware.py`; on unless `RATE_LIMIT_ENABLED` is not `true`),
answering 429 with `Retry-After`:

| Requests | Limit per minute |
|----------|------------------|
| Default (`RATE_LIMIT_DEFAULT`) | 100 |
| Any `GET` | 200 |
| `POST /ranges` | 30 |
| `POST /ranges/batch-provision` | 5 |
| `POST /ai/…` | 20 |
| Exercise Forge, quiz generation, AAR AI enhance, MESL generation | 10 each |
| Curriculum AI search and course generation, detection submissions | 30 each |

All users behind one address together get at most 10 times the per-user limit. The nginx
edge adds per-address `limit_req` zones and 100 connections per address
(`infra/platform/nginx/nginx.conf`). Keycloak's brute-force detection covers sign-in.

### Input Validation

API request bodies are Pydantic v2 models (`control-plane/api/app/schemas.py` and the
section modules), in the default (lax) mode. For example:

```python
class RangeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    template_id: uuid.UUID
```

**Validation rules applied:**
- **String lengths and numeric bounds** — where a model declares them; not on every field
- **UUID format** — path and body IDs typed as UUIDs
- **Enum values** — State/type fields restricted to valid enum values
- **No SQL injection** — SQLAlchemy parameterized queries throughout
- **No XSS** — Output encoding in templates; Angular auto-escapes
- **YAML validation** — Scenario YAML parsed with safe_load, schema-validated

---

## Audit Logging

### What Is Audited

Routers and services write `AuditLog` rows for their state changes: ranges (create, update,
delete, provision, power, destroy, abandon, snapshots), exercises (create, start, pause,
resume, complete, objective acknowledgement), detections, templates, scenarios and
executions, users, roster imports, teams, tenants, registrations, the scheduler, noise,
Greyspace, threat intel, detection rules, wiki and tickets. Coverage is per call site, not
enforced centrally, so a new endpoint is audited only if it writes a row.

**Not audited by TrueNorth:** sign-in success or failure, token refresh and sign-out (these
happen in Keycloak, and the realm does not enable Keycloak's event store), and reads.

### Audit Log Schema

`AuditLog` in `control-plane/api/app/models.py`:

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | |
| `timestamp` | timestamptz | server default `now()` |
| `user_id` | UUID, nullable | who acted |
| `tenant_id` | UUID, nullable | legacy rows may have none |
| `action` | varchar(100) | e.g. `create`, `provision`, `ack_objective` |
| `resource_type` | varchar(100) | e.g. `range`, `exercise` |
| `resource_id` | varchar(255) | |
| `detail` | text, nullable | free text |

Indexes: `(tenant_id, timestamp)` and `(resource_type, resource_id)`. The record has no
source IP address and no user agent.

### Querying Audit Logs

The only reader is a paged list, newest first: `GET /audit-log?limit=…&offset=…`
(`audit:read`; `limit` at most 500). Only the admin role holds `audit:read`. A caller who
also holds `tenant:read` sees every tenant's entries; the admin role holds every
permission, so in practice every admin does. The web app shows it under **Admin → Audit Log**. There is no
filtering by date, user or resource.

For anything else, query the database directly:

```sql
SELECT timestamp, user_id, action, resource_type, resource_id, detail
FROM audit_logs
WHERE tenant_id = '<tenant-id>'
  AND timestamp >= '2026-01-01' AND timestamp < '2026-02-01'
ORDER BY timestamp DESC;
```

### Audit Log Retention

- **Hot storage (PostgreSQL):** 90 days
- **Warm storage (export to S3/MinIO):** 365 days
- **Cold storage (archive):** 7 years (compliance)
- **Automated export:** Daily cron exports previous day's logs to MinIO

---

## Vulnerability Management

What CI actually runs (`.github/workflows/ci.yml`, `release.yml`): pip-audit and npm audit
on every run (blocking); Trivy, checksum-pinned, on each built image (fixable CRITICAL
blocks a PR; a release blocks on fixable HIGH and CRITICAL). The commands below are for
scanning by hand.

### Container Scanning

```bash
# Scan container images with Trivy
trivy image truenorth/control-plane-api:latest
trivy image truenorth/celery-worker:latest
trivy image truenorth/ai-orchestrator:latest
trivy image truenorth/scenario-engine:latest

# Scan in CI/CD pipeline (fail on HIGH/CRITICAL)
trivy image --exit-code 1 --severity HIGH,CRITICAL truenorth/control-plane-api:latest
```

### Dependency Scanning

```bash
# Python dependencies
pip-audit --requirement requirements.txt

# Node.js dependencies (frontend)
npm audit --production

# Go dependencies (if applicable)
govulncheck ./...
```

### Patch Management

A recommended cadence for operators; TrueNorth does not schedule or enforce it.

| Component | Update Frequency | Method |
|-----------|-----------------|--------|
| OS packages (containers) | Weekly | Rebuild base images |
| Python packages | Monthly (critical: immediate) | pip-compile + CI |
| Node.js packages | Monthly (critical: immediate) | npm update + CI |
| Keycloak | Quarterly | Helm upgrade |
| PostgreSQL | Quarterly | Rolling restart |
| OpenSearch | Quarterly | Rolling restart |

### Penetration Testing

No penetration test has been performed on TrueNorth. A recommended programme for an
operator: an external test before production and annually, covering the whole platform
including range VMs, with authentication bypass, privilege escalation, tenant isolation
and injection as focus areas.

---

## Security Hardening Checklist

### API Server

- [ ] TLS 1.2+ enforced on all endpoints
- [ ] CORS configured with specific allowed origins (not `*`)
- [ ] Security headers set (HSTS, X-Content-Type-Options, X-Frame-Options, CSP)
- [ ] Rate limiting enabled on all endpoints
- [ ] Request size limits enforced (default: 10 MB)
- [ ] JWT validation with signature verification
- [ ] All inputs validated via Pydantic models
- [ ] SQLAlchemy parameterized queries (no string formatting)
- [ ] Error messages do not leak internal details
- [ ] Debug mode disabled in production
- [ ] Health endpoints do not expose sensitive info

### Database

- [ ] Strong passwords (32+ chars, generated)
- [ ] TLS required for all connections
- [ ] Connection limited to application subnet only
- [ ] Regular VACUUM and REINDEX
- [ ] Backup encryption enabled
- [ ] pg_stat_statements enabled for query auditing
- [ ] Row-level security considered for multi-tenant tables

### Redis

- [ ] Authentication required (`requirepass`)
- [ ] TLS enabled
- [ ] Bind to internal interface only
- [ ] `CONFIG` command renamed/disabled
- [ ] Memory limit set with eviction policy
- [ ] No persistent storage of secrets

### OpenSearch

- [ ] Security plugin enabled
- [ ] TLS for transport and REST layers
- [ ] Role-based access for indices
- [ ] Audit logging enabled
- [ ] Index-per-range for tenant isolation

### MinIO

- [ ] TLS enabled
- [ ] IAM policies per tenant
- [ ] Bucket versioning enabled
- [ ] Server-side encryption enabled
- [ ] Access logging enabled

### Keycloak

- [ ] Admin console restricted to internal network
- [ ] Brute force protection enabled
- [ ] Password policy configured (see above)
- [ ] Session timeouts configured
- [ ] PKCE required for SPA client
- [ ] Token lifetimes minimized (1 hour access, 24 hour refresh)
- [ ] Unused realms and clients removed

### Infrastructure

- [ ] All nodes updated with latest security patches
- [ ] SSH key-based auth only (no password)
- [ ] Kubernetes RBAC configured (least privilege)
- [ ] Network policies enforced
- [ ] Pod security standards enforced
- [ ] Secrets stored in Vault or K8s secrets (encrypted)
- [ ] Container images from trusted registries only
- [ ] No containers running as root

---

## Compliance Mapping

Checked against `github/main` on 2026-10-12 (v1.2.0). This is a self-assessment of what the
code and shipped configuration do, not an accreditation, and it is not evidence that any
control has been assessed. **Implemented** means the code or shipped configuration does it;
**Partial** says exactly what is missing; **Not implemented** means nothing in the
repository does it, whatever an operator could add.

### NIST 800-53 Controls

| Control | Status | What exists, and the gap | Evidence |
|---------|--------|--------------------------|----------|
| AC-2 Account Management | Partial | Accounts are created only by approving an access request (instructor or admin); admins manage users. No automated disabling of inactive accounts; deprovisioning relies on the directory | `control-plane/api/app/routers/registration.py`, `control-plane/api/app/routers/admin.py`, `docs/onboarding.md` |
| AC-3 Access Enforcement | Implemented | 60 permissions over 5 roles, checked per route; tenant ownership checked on lookups | `control-plane/api/app/rbac.py`, `control-plane/api/app/tenancy.py` |
| AC-6 Least Privilege | Partial | Five roles with distinct permission sets. The admin role holds every permission, including cross-tenant reads | `control-plane/api/app/rbac.py` (`ROLE_PERMISSIONS`) |
| AC-7 Unsuccessful Logon Attempts | Implemented (Keycloak) | Brute-force detection after 5 failures, temporary lockout growing by 60 s up to 300 s; no permanent lockout. AD-federated accounts are also subject to the directory's own policy | `infra/keycloak/realm-truenorth.json`, `tests/contracts/test_keycloak_hardening.py` |
| AC-8 System Use Notification | Being built for v1.2.0; row owned by that work | Login banner via Keycloak theme | — |
| AC-12 Session Termination | Implemented (Keycloak) | Access token 5 min, SSO idle 30 min, SSO maximum 10 h, offline session 7 days | `install/roles/tn_keycloak/defaults/main.yml` (`tn_kc_realm_settings`) |
| AU-2 Event Logging | Partial | State changes in most sections write an audit row. Sign-ins, failures and reads are not logged by TrueNorth, and Keycloak's event store is not enabled | `control-plane/api/app/models.py` (`AuditLog`); [Audit Logging](#audit-logging) |
| AU-3 Content of Audit Records | Partial | Time, user, tenant, action, resource type and id, free-text detail. No source IP, user agent or outcome (success or failure) | `control-plane/api/app/models.py` (`AuditLog`) |
| AU-6 Audit Review | Partial | A paged, newest-first list (`GET /audit-log`, Admin → Audit Log). No search, filter, correlation, reporting or alerting on audit events | `control-plane/api/app/routers/admin.py` (`list_audit_log`) |
| AU-9 Protection of Audit Information | Being built for v1.2.0; row owned by that work | Append-only table, backup to MinIO | — |
| AU-11 Audit Record Retention | Being built for v1.2.0; row owned by that work | 90d hot, 365d warm, 7yr cold | — |
| CA-7 Continuous Monitoring | Partial | Prometheus, Alertmanager and Grafana with unit-tested alert rules. Alert delivery through Alertmanager has not been exercised on staging | `infra/platform/docker/compose.monitoring.yml`, `tests/contracts/test_prometheus_rules.py` |
| CA-8 Penetration Testing | Not implemented | No penetration test has been performed or scheduled. Internal code security sweeps only | — |
| CM-2 Baseline Configuration | Partial | Configuration as code: the Ansible installer, `compose.prod.yml` by image digest, a Helm chart, Packer image builds. No drift detection. (Terraform was removed in #131.) | `install/`, `infra/platform/docker/compose.prod.yml`, `infra/k8s/helm/`, `infra/vsphere/packer/` |
| CM-6 Configuration Settings | Partial | The installer enforces Keycloak token, session, password and client settings on every run. The [hardening checklist](#security-hardening-checklist) is guidance for the operator, not enforced | `install/roles/tn_keycloak/` |
| IA-2 Identification and Authentication | Partial | Keycloak OIDC with PKCE; optional read-only AD federation. MFA for privileged users: see the FedRAMP table. No CAC/PIV or smart-card support | `infra/keycloak/realm-truenorth.json`, `control-plane/api/app/identity.py` |
| IA-5 Authenticator Management | Partial | Local Keycloak accounts: minimum 12 characters, not the username, last 5 not reused. AD-federated passwords are governed by AD | `install/roles/tn_keycloak/defaults/main.yml` (`passwordPolicy`) |
| IR-4 Incident Handling | Partial | Written runbooks for incidents and on-call. No exercised incident on record | `docs/runbooks/incident.md`, `docs/runbooks/on-call.md` |
| IR-5 Incident Monitoring | Partial | Alert rules and runbook procedures exist; delivery unproven (see CA-7). No incident tracking system | `docs/operations.md`, `docs/runbooks/incident.md` |
| SC-8 Transmission Confidentiality | Partial | TLS 1.2/1.3 at the nginx edge with HSTS, and TLS to OpenSearch. Internal traffic to PostgreSQL, Redis, MinIO and Keycloak is plain on the container network | [Encryption in Transit](#encryption-in-transit) |
| SC-12 Cryptographic Key Management | Partial | One application key, `TN_SECRETS_KEY`, held in the env file, with a documented rotation (`new,old`) and escrow in the backups. No Vault, KMS or HSM | `control-plane/api/app/secretbox.py`, `docs/runbooks/key-rotation.md` |
| SC-13 Cryptographic Protection | Partial | Fernet (AES-128-CBC + HMAC-SHA256) for stored credentials; AES-256 with RSA key wrapping for the backup escrow. No FIPS 140-validated modules | `control-plane/api/app/secretbox.py`, `scripts/backup/backup.sh` |
| SC-28 Protection of Information at Rest | Partial | Only hypervisor credentials and AI engine keys are encrypted (sealed columns). No database TDE, no disk encryption (LUKS), no MinIO SSE, no OpenSearch at-rest encryption | [Encryption at Rest](#encryption-at-rest) |
| SI-2 Flaw Remediation | Implemented | Every CI run: pip-audit and npm audit (blocking), Trivy on built images (fixable CRITICAL blocks). Release: Trivy blocks fixable HIGH and CRITICAL | `.github/workflows/ci.yml`, `.github/workflows/release.yml` |
| SI-4 System Monitoring | Partial | Platform health and resource metrics (Prometheus exporters, host probes). No intrusion detection on the platform itself | `infra/platform/docker/compose.monitoring.yml` |
| SI-10 Information Input Validation | Implemented | Pydantic v2 request models; scenario YAML parsed safely and validated; telemetry search limited to a closed query language | `control-plane/api/app/schemas.py`, `control-plane/api/app/safe_yaml.py`, `control-plane/api/app/search_backends/query.py` |

### FedRAMP Alignment

For the FedRAMP Moderate baseline. TrueNorth is not FedRAMP authorized and no assessment has
been made.

| Area | Requirement | Status |
|------|------------|--------|
| Authentication | MFA for privileged users | Being built for v1.2.0; row owned by that work. Keycloak supports TOTP/WebAuthn |
| Encryption | FIPS 140-validated modules | **Not implemented.** Images use standard, non-FIPS crypto libraries |
| Audit | Continuous audit log export | Being built for v1.2.0; row owned by that work. Daily export to immutable storage |
| Monitoring | Continuous monitoring program | **Partial.** Prometheus, Alertmanager and Grafana; delivery not exercised (see CA-7) |
| Vulnerability Management | Monthly scanning | **Implemented** for code and images on every CI run and release (see SI-2). Deployed hosts are not scanned |
| Incident Response | Documented IR plan | **Partial.** Runbooks exist; never exercised (see IR-4) |
| Data Residency | Data within authorized boundaries | **Deployment-dependent.** Self-hosted: data stays on the hosts it is installed on. AI prompts go to the engine named by `OPENAI_BASE_URL` (required by `compose.prod.yml`). Two defaults point outside the install and must be changed for a closed boundary: the seeded `Ollama (Primary)` backend record (`control-plane/api/app/seed.py`, `seed_ai_backends`) and the orchestrator's `OLLAMA_PROXY_URL` default (`ai-orchestrator/app/main.py`), both `https://ai.guapo613.beer` |

### SOC 2 Type II Relevance

| Trust Service Criteria | What exists |
|----------------------|----------------|
| **Security** | RBAC, sealed credentials, edge TLS, audit rows for most state changes |
| **Availability** | Health checks, backups with a nightly CI restore drill. No multi-site or HA deployment |
| **Processing Integrity** | Input validation, state machines with transition guards |
| **Confidentiality** | Tenant scoping on lookups. Gaps: plain internal traffic, no at-rest encryption beyond credentials |
| **Privacy** | Minimal PII collection, data retention policies |