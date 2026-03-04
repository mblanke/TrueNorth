# TrueNorth Range — API Reference

> Complete API documentation for the TrueNorth Range Control Plane. All 42 endpoints with request/response examples, authentication, WebSocket protocol, and error handling.

---

## Table of Contents

- [Base URL and Versioning](#base-url-and-versioning)
- [Authentication](#authentication)
- [Error Response Format](#error-response-format)
- [Pagination](#pagination)
- [Rate Limiting](#rate-limiting)
- [Health](#health)
- [Tenants](#tenants)
- [Users](#users)
- [Teams](#teams)
- [Templates](#templates)
- [Scenarios](#scenarios)
- [Ranges](#ranges)
- [Exercises](#exercises)
- [Objectives](#objectives)
- [After Action Reports (AAR)](#after-action-reports-aar)
- [Telemetry](#telemetry)
- [Audit Log](#audit-log)
- [AI Orchestrator](#ai-orchestrator)
- [WebSocket Protocol](#websocket-protocol)
- [OpenAPI Specification](#openapi-specification)

---

## Base URL and Versioning

| Environment | Base URL | Notes |
|------------|----------|-------|
| Development | `http://localhost:8080` | `AUTH_DISABLED=true` |
| Staging | `https://staging.truenorth.local` | Keycloak auth required |
| Production | `https://api.truenorth.example.com` | Keycloak auth required |

The current API version is **v1** (implicit). All endpoints are served from the root path. Future versions will use `/v2/` prefix.

---

## Authentication

### Keycloak OIDC JWT Flow

All endpoints (except `GET /health`) require authentication via Bearer token.

**Token Acquisition:**

```bash
# 1. Get token from Keycloak
curl -X POST "http://keycloak:8180/realms/truenorth/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=truenorth-api" \
  -d "client_secret=<secret>" \
  -d "username=admin@truenorth.local" \
  -d "password=<password>"
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 300,
  "refresh_expires_in": 1800
}
```

**Using the Token:**
```bash
curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIs..." \
  http://localhost:8080/ranges
```

**JWT Claims Used:**
| Claim | Description |
|-------|-------------|
| `sub` | Keycloak user UUID |
| `email` | User email address |
| `preferred_username` | Display name |
| `realm_access.roles` | Role array (admin, instructor, student, observer, range_ops) |
| `tenant_id` | Custom claim for tenant isolation |

**Development Mode:**
Set `AUTH_DISABLED=true` to bypass authentication. All requests will use a default admin user.

---

## Error Response Format

All errors return a consistent JSON structure:

```json
{
  "detail": "Human-readable error message"
}
```

**Standard HTTP Status Codes:**

| Code | Meaning | Common Cause |
|------|---------|-------------|
| `400` | Bad Request | Invalid input or constraint violation |
| `401` | Unauthorized | Missing or invalid JWT |
| `403` | Forbidden | Insufficient permissions |
| `404` | Not Found | Resource does not exist |
| `409` | Conflict | State machine violation or duplicate |
| `422` | Unprocessable Entity | Pydantic validation failure |
| `429` | Too Many Requests | Rate limit exceeded |
| `500` | Internal Server Error | Unhandled exception |

**Validation Error (422):**
```json
{
  "detail": [
    {
      "loc": ["body", "name"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

---

## Pagination

List endpoints support cursor-based pagination:

| Parameter | Type | Default | Max | Description |
|-----------|------|---------|-----|-------------|
| `limit` | int | 50 | 200 | Number of items per page |
| `offset` | int | 0 | -- | Number of items to skip |

**Example:**
```bash
GET /ranges?limit=20&offset=40
```

---

## Rate Limiting

| Tier | Limit | Window | Applies To |
|------|-------|--------|-----------|
| Standard | 100 requests | Per minute | All authenticated users |
| Elevated | 500 requests | Per minute | Admin and instructor roles |
| Burst | 20 requests | Per second | Write operations (POST/PUT/DELETE) |
| AI | 10 requests | Per minute | AI Orchestrator endpoints |

Rate limit headers are included in every response:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1706000000
```

When exceeded, returns `429 Too Many Requests`:
```json
{
  "detail": "Rate limit exceeded. Retry after 45 seconds."
}
```

---

## Health

### `GET /health`

Returns platform health status. No authentication required.

**Response `200 OK`:**
```json
{
  "status": "ok",
  "version": "1.0.0",
  "db": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `ok` or `degraded` |
| `version` | string | API version |
| `db` | boolean | Database connectivity |

---

## Tenants

### `POST /tenants`

Create a new tenant organization. **Permission: `tenant:create`**

**Request:**
```json
{
  "name": "Acme Corporation",
  "slug": "acme"
}
```

**Response `201 Created`:**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "name": "Acme Corporation",
  "slug": "acme",
  "is_active": true,
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

| Status | Condition |
|--------|-----------|
| `201` | Tenant created successfully |
| `403` | User lacks `tenant:create` permission |
| `409` | Slug already exists |

---

### `GET /tenants`

List all tenants. **Permission: `tenant:read`**

**Response `200 OK`:**
```json
[
  {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "Acme Corporation",
    "slug": "acme",
    "is_active": true,
    "created_at": "2026-01-15T10:30:00Z",
    "updated_at": "2026-01-15T10:30:00Z"
  }
]
```

---

### `PUT /tenants/{tenant_id}`

Update a tenant. **Permission: `tenant:update`**

**Request:**
```json
{
  "name": "Acme Corp (Renamed)",
  "slug": "acme"
}
```

**Response `200 OK`:** Returns updated tenant object.

---

## Users

### `GET /users/me`

Return the currently authenticated user's profile. **Permission: any authenticated user**

**Response `200 OK`:**
```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "keycloak_id": "kc-uuid-here",
  "email": "admin@truenorth.local",
  "display_name": "Admin User",
  "role": "admin",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "is_active": true,
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

---

### `GET /users`

List users in the caller's tenant. **Permission: `user:read`**

**Response `200 OK`:** Array of user objects.

---

### `POST /users`

Provision a new user. **Permission: `user:create`**

**Request:**
```json
{
  "email": "trainee@acme.com",
  "display_name": "Jane Trainee",
  "role": "student",
  "keycloak_id": "kc-uuid-optional"
}
```

**Response `201 Created`:** Returns user object.

| Field | Required | Description |
|-------|----------|-------------|
| `email` | Yes | Unique email address |
| `display_name` | No | Defaults to email |
| `role` | No | One of: admin, instructor, student, observer, range_ops. Defaults to `student` |
| `keycloak_id` | No | Auto-generated if omitted |

---

### `DELETE /users/{user_id}`

Deactivate a user (soft delete). **Permission: `user:delete`**

**Response `204 No Content`**

---

## Teams

### `POST /teams`

Create a team. **Permission: `user:update`**

**Request:**
```json
{
  "name": "Blue Team Alpha"
}
```

**Response `201 Created`:**
```json
{
  "id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "name": "Blue Team Alpha",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

---

### `GET /teams`

List teams in the caller's tenant. **Permission: `user:read`**

**Response `200 OK`:** Array of team objects.

---

### `DELETE /teams/{team_id}`

Delete a team. **Permission: `user:update`**

**Response `204 No Content`**

---

## Templates

### `POST /templates`

Create a range template. **Permission: `template:create`**

**Request:**
```json
{
  "name": "Small Enterprise",
  "version": "1.0.0",
  "yaml": "id: small-enterprise\nname: Small Enterprise\nvms:\n  - name: dc01\n    os: windows-server-2022\n    role: domain-controller\n  - name: ws01\n    os: windows-11\n    role: workstation\nnetwork:\n  vlans:\n    - id: 100\n      name: corporate\n      cidr: 10.10.100.0/24",
  "is_public": true
}
```

**Response `201 Created`:**
```json
{
  "id": "d4e5f6a7-b8c9-0123-def0-234567890123",
  "name": "Small Enterprise",
  "version": "1.0.0",
  "yaml": "...",
  "is_public": true,
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Template display name |
| `version` | No | Semantic version, defaults to "0.1.0" |
| `yaml` | Yes | YAML definition of VMs, networks, sensors |
| `is_public` | No | If true, visible to all tenants |

---

### `GET /templates?limit=50&offset=0`

List templates visible to user (own tenant + public). **Permission: `template:read`**

**Response `200 OK`:** Array of template objects.

---

### `GET /templates/{template_id}`

Get template by ID. **Permission: `template:read`**

**Response `200 OK`:** Template object.

| Status | Condition |
|--------|-----------|
| `200` | Template found |
| `404` | Template not found |

---

### `PUT /templates/{template_id}`

Update a template. **Permission: `template:update`**

**Request:** Partial update (only changed fields required):
```json
{
  "name": "Small Enterprise v2",
  "version": "2.0.0",
  "yaml": "..."
}
```

**Response `200 OK`:** Updated template object.

| Status | Condition |
|--------|-----------|
| `200` | Updated successfully |
| `403` | Cannot update another tenant's template (non-admin) |
| `404` | Template not found |

---

### `DELETE /templates/{template_id}`

Delete a template. **Permission: `template:delete`**

**Response `204 No Content`**

---

## Scenarios

### `POST /scenarios`

Create a scenario. **Permission: `scenario:create`**

The `yaml` field is parsed and validated on creation.

**Request:**
```json
{
  "name": "Ransomware Lite",
  "version": "1.0.0",
  "yaml": "id: ransomware-lite\nname: Ransomware Lite\ndifficulty: intermediate\nduration_minutes: 45\ntimeline:\n  - id: event-1\n    type: email_phish\n    delay_minutes: 0\n    params:\n      subject: 'Invoice Attached'\nobjectives:\n  - ref_id: obj-1\n    title: Detect phishing email\n    type: detect\n    points: 25\n    validator:\n      type: opensearch_query\n      params:\n        query: 'event.action:email_received'\n        min_hits: 1",
  "is_public": true
}
```

**Response `201 Created`:**
```json
{
  "id": "e5f6a7b8-c9d0-1234-ef01-345678901234",
  "name": "Ransomware Lite",
  "version": "1.0.0",
  "yaml": "...",
  "is_public": true,
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

| Status | Condition |
|--------|-----------|
| `201` | Scenario created |
| `422` | Invalid YAML syntax or structure |

---

### `GET /scenarios?limit=50&offset=0`

List scenarios (own tenant + public). **Permission: `scenario:read`**

---

### `GET /scenarios/{scenario_id}`

Get scenario by ID. **Permission: `scenario:read`**

---

### `PUT /scenarios/{scenario_id}`

Update a scenario. YAML is re-validated on update. **Permission: `scenario:update`**

---

### `DELETE /scenarios/{scenario_id}`

Delete a scenario. **Permission: `scenario:delete`**

**Response `204 No Content`**

---

## Ranges

### `POST /ranges`

Create a new range in `created` state. **Permission: `range:create`**

**Request:**
```json
{
  "name": "Training Range Alpha",
  "template_id": "d4e5f6a7-b8c9-0123-def0-234567890123"
}
```

**Response `201 Created`:**
```json
{
  "id": "f6a7b8c9-d0e1-2345-f012-456789012345",
  "name": "Training Range Alpha",
  "template_id": "d4e5f6a7-b8c9-0123-def0-234567890123",
  "state": "created",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "provisioner_backend": "mock",
  "provisioner_output": null,
  "error_message": null,
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

| Status | Condition |
|--------|-----------|
| `201` | Range created |
| `404` | Template not found |

---

### `GET /ranges?limit=50&offset=0`

List ranges for the user's tenant. **Permission: `range:read`**

**Response `200 OK`:** Array of range list objects.

---

### `GET /ranges/stats`

Aggregate range statistics for the tenant. **Permission: `stats:read`**

**Response `200 OK`:**
```json
{
  "total_ranges": 15,
  "by_state": {
    "created": 2,
    "ready": 5,
    "running": 3,
    "destroyed": 5
  },
  "total_vms": 0,
  "active_exercises": 2
}
```

---

### `GET /ranges/{range_id}`

Get range by ID. **Permission: `range:read`**

**Response `200 OK`:** Range object.

---

### `PUT /ranges/{range_id}`

Update range metadata. **Permission: `range:update`**

---

### `DELETE /ranges/{range_id}`

Delete a range record. **Permission: `range:delete`**

**Response `204 No Content`**

---

### `POST /ranges/{range_id}/provision`

Trigger asynchronous range provisioning. Transitions state from `created` to `provisioning`. A Celery task is dispatched.

**Permission: `range:provision`**

**Response `200 OK`:** Range object with `state: "provisioning"`.

| Status | Condition |
|--------|-----------|
| `200` | Provisioning initiated |
| `404` | Range not found |
| `409` | Invalid state transition (range not in provisionable state) |

---

### `POST /ranges/{range_id}/destroy`

Trigger asynchronous range destruction. Transitions to `destroying` state.

**Permission: `range:destroy`**

**Response `200 OK`:** Range object with `state: "destroying"`.

| Status | Condition |
|--------|-----------|
| `200` | Destruction initiated |
| `404` | Range not found |
| `409` | Invalid state transition |

---

### `POST /ranges/{range_id}/stop`

Stop a running range. Transitions from `running` to `stopped`.

**Permission: `range:provision`**

**Response `200 OK`:** Range object with `state: "stopped"`.

---

### `POST /ranges/batch-provision`

Batch-provision multiple ranges simultaneously. Returns immediately with a task ID.

**Permission: `range:batch_provision`**

**Request:**
```json
{
  "range_ids": [
    "f6a7b8c9-d0e1-2345-f012-456789012345",
    "a1234567-b890-cdef-1234-567890abcdef"
  ]
}
```

**Response `202 Accepted`:**
```json
{
  "dispatched": 2,
  "task_id": "celery-task-uuid-here"
}
```

| Status | Condition |
|--------|-----------|
| `202` | Batch task dispatched |
| `400` | One or more range IDs not found |
| `409` | One or more ranges in non-provisionable state |

---

## Exercises

### `POST /exercises`

Create a new exercise. **Permission: `exercise:create`**

**Request:**
```json
{
  "name": "IR Drill Alpha",
  "range_id": "f6a7b8c9-d0e1-2345-f012-456789012345",
  "scenario_id": "e5f6a7b8-c9d0-1234-ef01-345678901234",
  "max_score": 100
}
```

**Response `201 Created`:**
```json
{
  "id": "a7b8c9d0-e1f2-3456-0123-567890123456",
  "name": "IR Drill Alpha",
  "range_id": "f6a7b8c9-d0e1-2345-f012-456789012345",
  "scenario_id": "e5f6a7b8-c9d0-1234-ef01-345678901234",
  "state": "pending",
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "started_at": null,
  "completed_at": null,
  "total_score": 0,
  "max_score": 100,
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

---

### `GET /exercises?limit=50&offset=0`

List exercises for user's tenant. **Permission: `exercise:read`**

---

### `GET /exercises/{exercise_id}`

Get exercise by ID. **Permission: `exercise:read`**

---

### `POST /exercises/{exercise_id}/start`

Start a pending exercise. Transitions from `pending` to `running`. Sets `started_at`.

**Permission: `exercise:start`**

| Status | Condition |
|--------|-----------|
| `200` | Exercise started |
| `404` | Exercise not found |
| `409` | Exercise not in `pending` state |

---

### `POST /exercises/{exercise_id}/pause`

Pause a running exercise. Transitions from `running` to `paused`.

**Permission: `exercise:pause`**

| Status | Condition |
|--------|-----------|
| `200` | Exercise paused |
| `409` | Exercise not in `running` state |

---

### `POST /exercises/{exercise_id}/complete`

Complete an exercise. Tallies all achieved objective scores. Sets `completed_at`.

**Permission: `exercise:complete`**

**Response `200 OK`:**
```json
{
  "id": "a7b8c9d0-e1f2-3456-0123-567890123456",
  "name": "IR Drill Alpha",
  "state": "completed",
  "total_score": 75,
  "max_score": 100,
  "started_at": "2026-01-15T10:30:00Z",
  "completed_at": "2026-01-15T11:30:00Z"
}
```

---

## Objectives

### `GET /exercises/{exercise_id}/objectives`

List objectives for an exercise. **Permission: `exercise:read`**

**Response `200 OK`:**
```json
[
  {
    "id": "b8c9d0e1-f2a3-4567-1234-678901234567",
    "exercise_id": "a7b8c9d0-e1f2-3456-0123-567890123456",
    "ref_id": "obj-1",
    "objective_type": "detection",
    "description": "Detect the phishing email",
    "validator": "opensearch_query",
    "validator_params": "{\"query\": \"event.action:email_received\", \"min_hits\": 1}",
    "points": 25,
    "achieved": false,
    "evidence": null,
    "achieved_at": null
  }
]
```

---

### `POST /exercises/{exercise_id}/objectives/{ref_id}/ack`

Acknowledge (achieve) an objective. Sets `achieved=true` with evidence and timestamp.

**Permission: `exercise:complete`**

**Request:**
```json
{
  "evidence": "Screenshot of detection alert in SIEM dashboard"
}
```

**Response `200 OK`:**
```json
{
  "id": "b8c9d0e1-f2a3-4567-1234-678901234567",
  "ref_id": "obj-1",
  "achieved": true,
  "evidence": "Screenshot of detection alert in SIEM dashboard",
  "achieved_at": "2026-01-15T10:45:00Z",
  "points": 25
}
```

| Status | Condition |
|--------|-----------|
| `200` | Objective acknowledged |
| `404` | Objective not found |
| `409` | Objective already achieved |

---

## After Action Reports (AAR)

### `POST /exercises/{exercise_id}/aar/generate`

Generate an After Action Report from exercise data. Creates or updates the AAR for the exercise.

**Permission: `aar:generate`**

**Response `201 Created`:**
```json
{
  "id": "c9d0e1f2-a3b4-5678-2345-789012345678",
  "exercise_id": "a7b8c9d0-e1f2-3456-0123-567890123456",
  "report_json": "{\"exercise\": {...}, \"scores\": {\"total\": 75, \"max\": 100, \"pct\": 75.0}, \"objectives\": [...]}",
  "report_html": "<html><body><h1>AAR: IR Drill Alpha</h1>...</body></html>",
  "generated_at": "2026-01-15T12:00:00Z"
}
```

---

### `GET /exercises/{exercise_id}/aar`

Retrieve AAR as JSON. **Permission: `aar:read`**

**Response `200 OK`:** AAR object.

---

### `GET /exercises/{exercise_id}/aar/html`

Retrieve AAR as rendered HTML. **Permission: `aar:read`**

**Response `200 OK`:** `Content-Type: text/html`

```html
<html><body>
  <h1>AAR: IR Drill Alpha</h1>
  <p>Score: 75.0%</p>
  <!-- Full rendered report -->
</body></html>
```

---

## Telemetry

### `POST /telemetry/{range_id}/events`

Ingest telemetry events into the range's OpenSearch index. Events are tagged with `range_id` and `tenant_id`.

**Request:**
```json
{
  "events": [
    {
      "timestamp": "2026-01-15T10:30:00Z",
      "event.action": "process_create",
      "process.name": "powershell.exe",
      "process.command_line": "powershell -enc ...",
      "host.name": "ws01",
      "source": "sysmon"
    }
  ]
}
```

**Response `201 Created`:**
```json
{
  "indexed": 1,
  "index": "tn-range-f6a7b8c9-events"
}
```

---

### `GET /telemetry/{range_id}/search?q=*&size=50`

Search telemetry events for a range using OpenSearch query syntax.

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | `*` | OpenSearch query string |
| `size` | int | 50 | Max results |
| `from_ts` | string | -- | Start timestamp (ISO 8601) |
| `to_ts` | string | -- | End timestamp (ISO 8601) |

**Response `200 OK`:**
```json
{
  "total": 1250,
  "hits": [
    {
      "_id": "abc123",
      "_source": {
        "timestamp": "2026-01-15T10:30:00Z",
        "event.action": "process_create",
        "process.name": "powershell.exe"
      }
    }
  ]
}
```

---

## Audit Log

### `GET /audit-log`

Retrieve audit log entries. **Permission: `audit:read`**

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results |
| `offset` | int | 0 | Pagination offset |
| `resource_type` | string | -- | Filter by resource type |
| `action` | string | -- | Filter by action |

**Response `200 OK`:**
```json
[
  {
    "id": "d0e1f2a3-b4c5-6789-3456-890123456789",
    "timestamp": "2026-01-15T10:30:00Z",
    "user_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "action": "create",
    "resource_type": "range",
    "resource_id": "f6a7b8c9-d0e1-2345-f012-456789012345",
    "detail": ""
  }
]
```

---

## AI Orchestrator

The AI Orchestrator runs as a separate service on port 6000. It provides LLM-powered capabilities.

### `POST /ai/generate`

General-purpose text generation. **No authentication** (internal service).

**Request:**
```json
{
  "prompt": "Explain the MITRE ATT&CK technique T1566.001",
  "model": "gpt-4o-mini",
  "max_tokens": 500
}
```

**Response `200 OK`:**
```json
{
  "text": "T1566.001 (Spearphishing Attachment) is a technique where adversaries...",
  "model": "gpt-4o-mini",
  "usage": {
    "prompt_tokens": 15,
    "completion_tokens": 120
  }
}
```

---

### `POST /ai/detection-rule`

Generate a Sigma-style detection rule from a threat description.

**Request:**
```json
{
  "description": "Detect PowerShell encoded command execution",
  "data_source": "sysmon",
  "severity": "high"
}
```

**Response `200 OK`:**
```json
{
  "rule": "title: PowerShell Encoded Command\nstatus: experimental\nlogsource:\n  product: windows\n  service: sysmon\ndetection:\n  selection:\n    process.name: powershell.exe\n    process.command_line|contains: '-enc'\n  condition: selection\nlevel: high",
  "model": "gpt-4o",
  "mitre_techniques": ["T1059.001"]
}
```

---

### `POST /ai/scenario-suggest`

Suggest modifications or variations to an existing scenario.

**Request:**
```json
{
  "scenario_yaml": "id: apt-breach\ntimeline:\n  - id: event-1\n    type: email_phish\n    ...",
  "difficulty": "advanced",
  "focus": "lateral_movement"
}
```

**Response `200 OK`:**
```json
{
  "suggestions": [
    {
      "description": "Add lateral movement via PsExec after initial access",
      "new_event": {
        "id": "event-3",
        "type": "simulated_execution",
        "delay_minutes": 20,
        "depends_on": "event-2",
        "params": {
          "technique": "T1570",
          "description": "Lateral tool transfer via PsExec"
        }
      }
    }
  ],
  "model": "gpt-4o"
}
```

---

### `POST /ai/aar-analysis`

Analyze exercise data and generate narrative insights for the AAR.

**Request:**
```json
{
  "exercise_id": "a7b8c9d0-e1f2-3456-0123-567890123456",
  "scores": { "total": 75, "max": 100 },
  "objectives": [...],
  "telemetry_summary": { "total_events": 5000, "alerts_triggered": 12 }
}
```

**Response `200 OK`:**
```json
{
  "analysis": "The team demonstrated strong initial detection capabilities, identifying the phishing email within 5 minutes. However, lateral movement activity went undetected for 25 minutes...",
  "recommendations": [
    "Improve endpoint monitoring for process creation events",
    "Add detection rules for PsExec lateral movement"
  ],
  "model": "gpt-4o"
}
```

---

## WebSocket Protocol

TrueNorth Range provides real-time updates via WebSocket connections. The WebSocket manager supports up to **10,000 concurrent connections** with Redis pub/sub for multi-instance broadcasting.

### Connection

```
ws://localhost:8080/ws?token=<jwt_token>
```

### Message Format

All WebSocket messages use structured JSON:

```json
{
  "type": "range_state",
  "channel": "range:f6a7b8c9-d0e1-2345-f012-456789012345",
  "seq": 42,
  "timestamp": "2026-01-15T10:30:00.123Z",
  "payload": {
    "range_id": "f6a7b8c9-d0e1-2345-f012-456789012345",
    "state": "ready",
    "previous_state": "provisioning"
  }
}
```

### Message Types

| Type | Description | Payload |
|------|-------------|---------|
| `range_state` | Range state transition | `range_id`, `state`, `previous_state` |
| `exercise_update` | Exercise state or score change | `exercise_id`, `state`, `total_score` |
| `scenario_event` | Scenario injector fired | `exercise_id`, `event_id`, `type` |
| `system_notification` | System-wide announcement | `message`, `severity` |
| `telemetry_stream` | Live telemetry event | `range_id`, `event` |

### Channel Subscription

Subscribe to specific channels after connecting:

```json
{
  "action": "subscribe",
  "channels": ["range:f6a7b8c9", "exercise:a7b8c9d0"]
}
```

### Heartbeat

The server sends a ping every 30 seconds. Clients must respond with a pong within 3 cycles (90 seconds) or the connection is terminated.

### Connection Limits

| Limit | Value |
|-------|-------|
| Per-user connections | 50 |
| Global connections | 10,000 |
| Heartbeat interval | 30 seconds |
| Max missed pongs | 3 |

---

## OpenAPI Specification

The auto-generated OpenAPI specification is available at:

| Format | URL |
|--------|-----|
| Interactive (Swagger UI) | `http://localhost:8080/docs` |
| ReDoc | `http://localhost:8080/redoc` |
| Raw JSON | `http://localhost:8080/openapi.json` |

---

## Complete Endpoint Summary

| # | Method | Path | Permission | Description |
|---|--------|------|-----------|-------------|
| 1 | `GET` | `/health` | None | Health check |
| 2 | `POST` | `/tenants` | `tenant:create` | Create tenant |
| 3 | `GET` | `/tenants` | `tenant:read` | List tenants |
| 4 | `PUT` | `/tenants/{id}` | `tenant:update` | Update tenant |
| 5 | `GET` | `/users/me` | Authenticated | Current user profile |
| 6 | `GET` | `/users` | `user:read` | List users |
| 7 | `POST` | `/users` | `user:create` | Create user |
| 8 | `DELETE` | `/users/{id}` | `user:delete` | Delete user |
| 9 | `POST` | `/teams` | `user:update` | Create team |
| 10 | `GET` | `/teams` | `user:read` | List teams |
| 11 | `DELETE` | `/teams/{id}` | `user:update` | Delete team |
| 12 | `POST` | `/templates` | `template:create` | Create template |
| 13 | `GET` | `/templates` | `template:read` | List templates |
| 14 | `GET` | `/templates/{id}` | `template:read` | Get template |
| 15 | `PUT` | `/templates/{id}` | `template:update` | Update template |
| 16 | `DELETE` | `/templates/{id}` | `template:delete` | Delete template |
| 17 | `POST` | `/scenarios` | `scenario:create` | Create scenario |
| 18 | `GET` | `/scenarios` | `scenario:read` | List scenarios |
| 19 | `GET` | `/scenarios/{id}` | `scenario:read` | Get scenario |
| 20 | `PUT` | `/scenarios/{id}` | `scenario:update` | Update scenario |
| 21 | `DELETE` | `/scenarios/{id}` | `scenario:delete` | Delete scenario |
| 22 | `POST` | `/ranges` | `range:create` | Create range |
| 23 | `GET` | `/ranges` | `range:read` | List ranges |
| 24 | `GET` | `/ranges/stats` | `stats:read` | Range statistics |
| 25 | `GET` | `/ranges/{id}` | `range:read` | Get range |
| 26 | `PUT` | `/ranges/{id}` | `range:update` | Update range |
| 27 | `DELETE` | `/ranges/{id}` | `range:delete` | Delete range |
| 28 | `POST` | `/ranges/{id}/provision` | `range:provision` | Provision range |
| 29 | `POST` | `/ranges/{id}/destroy` | `range:destroy` | Destroy range |
| 30 | `POST` | `/ranges/{id}/stop` | `range:provision` | Stop range |
| 31 | `POST` | `/ranges/batch-provision` | `range:batch_provision` | Batch provision |
| 32 | `POST` | `/exercises` | `exercise:create` | Create exercise |
| 33 | `GET` | `/exercises` | `exercise:read` | List exercises |
| 34 | `GET` | `/exercises/{id}` | `exercise:read` | Get exercise |
| 35 | `POST` | `/exercises/{id}/start` | `exercise:start` | Start exercise |
| 36 | `POST` | `/exercises/{id}/pause` | `exercise:pause` | Pause exercise |
| 37 | `POST` | `/exercises/{id}/complete` | `exercise:complete` | Complete exercise |
| 38 | `GET` | `/exercises/{id}/objectives` | `exercise:read` | List objectives |
| 39 | `POST` | `/exercises/{id}/objectives/{ref}/ack` | `exercise:complete` | Ack objective |
| 40 | `POST` | `/exercises/{id}/aar/generate` | `aar:generate` | Generate AAR |
| 41 | `GET` | `/exercises/{id}/aar` | `aar:read` | Get AAR JSON |
| 42 | `GET` | `/exercises/{id}/aar/html` | `aar:read` | Get AAR HTML |
| 43 | `POST` | `/telemetry/{range_id}/events` | `telemetry:read` | Ingest events |
| 44 | `GET` | `/telemetry/{range_id}/search` | `telemetry:read` | Search events |
| 45 | `GET` | `/audit-log` | `audit:read` | Audit log |
| 46 | `POST` | `/ai/generate` | Internal | AI text gen |
| 47 | `POST` | `/ai/detection-rule` | Internal | Generate detection |
| 48 | `POST` | `/ai/scenario-suggest` | Internal | Scenario suggest |
| 49 | `POST` | `/ai/aar-analysis` | Internal | AAR analysis |
| -- | `WS` | `/ws` | Authenticated | WebSocket |