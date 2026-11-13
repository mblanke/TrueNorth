# TrueNorth Range — API Reference

> API documentation for the TrueNorth Range Control Plane. Core endpoints with request/response examples, authentication, WebSocket protocol, and error handling. The complete machine-readable contract is [`docs/interfaces/openapi.json`](interfaces/openapi.json) — 465 paths / 593 operations; this guide covers the core ones.

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
| Development | `http://localhost:4200/api` | nginx front door (`/api/` → the API); `AUTH_DISABLED=true` in the dev compose. API direct: `http://127.0.0.1:8081` (`compose.dev.yml`) |
| Staging | `https://staging.truenorth.local` | Keycloak auth required |
| Production | `https://api.truenorth.example.com` | Keycloak auth required |

The current API version is **v1**, served at `/api/v1/...` (through nginx on `:4200`).
Unversioned paths (`/api/ranges`) remain aliases of v1 for existing clients. Every
response carries `X-API-Version`. A breaking change will ship as `/api/v2` alongside v1.
The machine-readable contract is [`docs/interfaces/openapi.json`](interfaces/openapi.json)
and is checked against the code on every `scripts/dod.sh` run (ADR 0002).

---

## Authentication

### Keycloak OIDC JWT Flow

All endpoints (except `GET /health`) require authentication via Bearer token.

**Token acquisition.** The OAuth password grant (direct access grants) is **off** on
every client people use: `truenorth-web`, `truenorth-api` and `truenorth-cli`
(`infra/keycloak/realm-truenorth.json`; `install/playbooks/60-keycloak.yml` enforces it on
every run). Do not script `grant_type=password`; Keycloak refuses it.

| Client | Flow | Used by |
|---|---|---|
| `truenorth-web` (public) | Authorization code + PKCE (S256) | The web app. Sign in through the browser; the SPA refreshes its 5-minute access token silently. |
| `truenorth-cli` (public) | OAuth device authorization grant | Scripts and terminals: approve the device code in a browser. |
| `truenorth-api` (confidential) | None for people (service account only) | The API's own token audience (`KEYCLOAK_AUDIENCE`). |
| `truenorth-smoke` (confidential) | Password grant | `install/playbooks/95-smoke-test.yml` only. The client is **disabled**; the smoke test (`install/roles/tn_smoke`) enables it for its one token request and disables it again. |

Token from a terminal with the device grant (`<issuer>` is `KEYCLOAK_ISSUER`, by default
`https://<site>/auth/realms/truenorth`):

```bash
# 1. Ask for a device code; open verification_uri_complete in a browser and sign in
curl -s -X POST "<issuer>/protocol/openid-connect/auth/device" \
  -d "client_id=truenorth-cli"

# 2. Poll for the token with the device_code from step 1
curl -s -X POST "<issuer>/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
  -d "client_id=truenorth-cli" \
  -d "device_code=<device_code>"
```

**Response (abridged):**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 300
}
```

**Using the Token:**
```bash
curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIs..." \
  https://<site>/api/v1/ranges
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
With `TN_ENV=production` the API refuses to start if `AUTH_DISABLED=true`
(`control-plane/api/app/settings.py`).

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

List endpoints use limit/offset pagination:

| Parameter | Type | Default | Max | Description |
|-----------|------|---------|-----|-------------|
| `limit` | int | 50 | 200 | Number of items per page |
| `offset` | int | 0 | -- | Number of items to skip |

**Example:**
```bash
GET /ranges?limit=20&offset=40
```

---

A sliding-window limiter (Redis, 60 s window, `app/middleware.py`) keys on the caller's
token subject and address. There are no role tiers; limits are per route:

| Limit | Applies To |
|-------|-----------|
| 100 requests / min | Default for every authenticated route |
| 200 requests / min | All `GET` endpoints |
| 30 / min | `POST /ranges` |
| 5 / min | `POST /ranges/batch-provision` |
| 20 / min | `POST /ai/*` |
| 30 / min | `POST /curricula/{id}/search`, `POST /curricula/{id}/generate-course` |
| 10 / min | `POST /exercise-forge`, `POST /quizzes/generate`, AAR ai-enhance, MESL generate |
| 30 / min | `POST /exercises/{id}/objectives/{ref}/detections` |
| Unlimited | `GET /health` |

`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` are on every
rate-limited response:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1706000000
```

When exceeded, returns `429 Too Many Requests` (retry window = the 60 s window):
```json
{
  "detail": "Rate limit exceeded. Try again later.",
  "retry_after": 60
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
  "app": "truenorth-range",
  "db": true,
  "redis": true
}
```

`503 Service Unavailable` with `"status": "unavailable"` when the database is down.

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `ok` (200) or `unavailable` (503) |
| `version` | string | API version |
| `app` | string | Always `truenorth-range` |
| `db` | boolean | Database connectivity |
| `redis` | boolean | Redis connectivity |

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
| `display_name` | Yes | Display name (at least 1 character) |
| `role` | No | One of: admin, instructor, student, observer, range_ops. Defaults to `student` |
| `keycloak_id` | No | Auto-generated if omitted |

---

### `DELETE /users/{user_id}`

Deactivate a user (soft delete). **Permission: `user:delete`**

**Response `204 No Content`**

---

## Teams

### `POST /teams`

Create a team. **Permission: `user:update`**, or **`team:manage_own`** with `exercise_id`
naming an exercise you run (#200; `docs/security.md`, "An instructor's own class").
Without `exercise_id` an instructor gets 403; another tenant's exercise is 404.

**Request:**
```json
{
  "name": "Blue Team Alpha",
  "exercise_id": "7d1e0f52-3a8b-4c6d-9e0f-1a2b3c4d5e6f"
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

Delete a team. **Permission: `user:update`**, or **`team:manage_own`** for a team of an
exercise you run. `PATCH /teams/{id}` and the member routes follow the same rule; with
`team:manage_own` only Students are added or removed.

**Response `204 No Content`**

---

### `GET /class-setup/mine`

The courses you teach and the exercises you run (#200). **Permission:
`enrollment:manage_own` or `team:manage_own`**

**Response `200 OK`:**
```json
{
  "courses": [{"id": "21ed2043-5788-41a7-8d80-1a05a95c1a2c", "name": "SOC Analyst I"}],
  "exercises": [{"id": "7d1e0f52-3a8b-4c6d-9e0f-1a2b3c4d5e6f", "name": "Op Northwind"}]
}
```

---

### `POST /courses/{course_id}/withdraw/{user_id}`

Withdraw a Student from a course; the enrolment is kept with status `withdrawn`
(`POST /courses/{id}/enroll` by staff reopens it). **Permission: `user:update`**, or
**`enrollment:manage_own`** for a Student on a course you teach. 404 for another tenant's
course or person or no enrolment; 409 for a completed or failed one. Audited.

**Response `200 OK`:** the enrolment, `"status": "withdrawn"`.

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
| `version` | No | Semantic version, defaults to "1.0" |
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

### Range operations: provision, destroy, stop, start

Each of these is an **operation** (app/range_ops): the request is recorded in the same
transaction as the range's move to its in-progress state, and the worker writes the
outcome state once the hypervisor has done the work. `202 Accepted` means *recorded*,
not done. The body is the range as it is now; the `Operation-Id` header names the
operation, whose progress is at `GET /ranges/{range_id}/operations/{operation_id}`.

- **Task queue down:** still `202`. The operation stays `pending` with
  `error.code = "broker_unavailable"`, and the API re-sends it once the broker is back.
- **`Idempotency-Key` header** (optional): the same key for the same action returns the
  original operation and does nothing again. The same key for another action is a `409`.
- **One operation at a time per range:** a second action while one is in flight is a
  `409`. A **destroy supersedes** an in-flight operation instead, so a range whose task
  was lost can always be torn down.

| Endpoint | Permission | From | In progress | Outcome |
|---|---|---|---|---|
| `POST /ranges/{range_id}/provision` | `range:provision` | `created`, `failed` (no VMs recorded) | `provisioning` | `ready` / `failed` |
| `POST /ranges/{range_id}/destroy` | `range:destroy` | most states, including `stopping` / `starting` | `destroying` | `destroyed` / `failed` |
| `POST /ranges/{range_id}/stop` | `range:provision` | `ready`, `running` | `stopping` | `stopped` / `failed` |
| `POST /ranges/{range_id}/start` | `range:provision` | `stopped` | `starting` | `running` / `failed` |

Stop and start are refused (`409`) for a range with no recorded VMs, or while a snapshot
is being taken or restored. Stop is also refused while an exercise is running on the
range. No new operation (other than a destroy) is accepted while a worker task still holds
the range's lease, e.g. one that was abandoned. A snapshot or restore is refused while an
operation is in flight. vSphere's stop is a hard power-off, not a guest shutdown.

| Status | Condition |
|--------|-----------|
| `202` | Accepted (recorded; see the operation for progress) |
| `404` | Range not found (or another tenant's) |
| `409` | Not allowed in the range's state, another operation in flight, a lab session's range, or an Idempotency-Key reused for another action |

### `GET /ranges/{range_id}/operations`, `GET /ranges/{range_id}/operations/{operation_id}`

**Permission: `range:read`.** The range's operations, newest first, with their outcomes
reconciled from its state:

- `status` is `pending`, `dispatched`, `held`, `succeeded`, `failed` or `superseded`.
- `error.code` is `broker_unavailable`, `range_failed`, `no_outcome`, `abandoned` or
  `superseded`.

`no_outcome` means no result arrived long after the task was sent. The operation still
blocks the range until someone abandons it.

### `POST /ranges/{range_id}/operations/{operation_id}/abandon`

**Permission: `range:destroy`.** An operator gives up on an in-flight operation that will
not finish (a lost task, a dead worker). Check the hypervisor first. The operation becomes
`failed` (`abandoned`), and the range goes to `failed`, from where it can be destroyed or
provisioned again. The task's lease becomes a tombstone that blocks the range for up to
`RANGE_LEASE_SECONDS` if the worker is dead, or until its in-flight work ends if it is
alive (at worst about 1 h 55 min for a hung worker). See docs/operations.md, "Range
Leases, Abandon and Recovery".

### `POST /ranges/{range_id}/lease/force-release`

**Permission: `range:lease_force_release` (admin only). Dangerous.** Deletes an abandoned
operation's lease tombstone so the range can be acted on at once. Body:
`{"confirm_range_id": "<range id again>", "reason": "<10-500 characters>"}`. Returns
`{range_id, released_holder, was_expired, warning}`. 404 when the range (in your tenant)
has no lease; 409 when the lease is a live task's, not a tombstone; 422 when
`confirm_range_id` differs. Audited as `force_release_lease`. If the abandoned worker is
in fact still running, its work continues beside whatever runs next.

### `GET /ranges/{range_id}/credentials`

**Permission: `range:read`, then by role (ADR 0012 §13, ADR 0013 §3).** The range's
accounts, **without passwords**: the ones its template declares (`accounts:`) and the
implicit staff logins TrueNorth adds (`source: "implicit"`: the built-in Administrator, the
Linux build user, Security Onion's `soadmin`), every one with a password generated for this
range, except a scenario account's typed-in one (`scenario: true`, never rotated).

- `range:credentials_read` (admin, instructor, range_ops): every account, `scope: "all"`;
  `applied: false` marks one the last build or rotation did not set (Windows accounts
  other than `Administrator`, appliances, non-vSphere backends, a pending rotation, or not
  built yet);
- a Student: only `student_visible` accounts with `applied: true`, `scope: "student"`, of a
  range they take part in (an exercise running or paused on it that is booked to a course
  they are rostered on, or their own live lab session). Any other range is 404;
- an observer (any other role): 403. Another tenant's range: 404.

```json
{"range_id": "7b1f…", "scope": "student",
 "accounts": [{"id": "c0a8…", "machine": "jump01", "username": "analyst", "role": "user",
               "source": "declared", "student_visible": true, "generated": true, "scenario": false,
               "applied": true, "setup_status": "applied", "rotated_at": null, "notes": "The way in"}]}
```

### `POST /ranges/{range_id}/credentials/{credential_id}/reveal`

**Permission: as the list, for that one account** (a Student: only one handed to them and
applied; anything else 404). Returns `{"id", "machine", "username", "password"}` with
`Cache-Control: no-store`. Every call writes an audit row `range_credential_reveal` whose
detail is `{range_id, machine, username, scope, via, exercise_id | lab_session_id}`; never a
password. While a change of the account is pending (`pending_change` in the list) it returns
the password the guests still have, flagged `pending_change` in the audit row: a reveal never
shows a password that does not log in. The wiki never shows passwords (ADR 0013 §3).

### `POST /ranges/{range_id}/credentials/{credential_id}/rotate`

**Permission: `range:credentials_rotate` (admin).** Body `{"reason": "..."}` (3-500
characters). Gives the account a new generated password. `202` with
`{"account": {...}, "applies": "in_guest" | "next_build", "operation_id", "dispatched"}`:
a range that is `ready` or `running` gets it in its guests now (worker task
`rotate_range_credentials`; the account shows `applied` once a login with it worked), an
unbuilt one (`created`, `destroyed`) at its next build. `409` for a scenario account, an
account whose previous change is still pending (one change at a time), a Security Onion or
appliance machine or a backend other than `vsphere_api` (the worker cannot change those
guests; rebuild instead), a range in any other state, an operation in flight or a restore in
progress. Audited as `range_credential_rotate`
`{range_id, machine, username, reason, operation_id, in_guest}`. The worker settles the change
on the version it set: `applied` once a login with the new password worked, otherwise the
previous password (which the guests still have) becomes the password again; either way an
audit row `range_credential_change` `{operation_id, outcome, accounts, why}`.

### `POST /ranges/{range_id}/credentials/{credential_id}/settle`

**Permission: `range:credentials_rotate` (admin).** Body `{"reason": "..."}`. Ends a change
that is stuck pending (its worker task died, its restore never ran): the account's password
becomes the one the guests have now (the previous one; after a restore's revert, the one the
snapshot recorded), applied and rotatable again. `409` when nothing is pending. Audited as
`range_credential_settle` `{range_id, machine, username, reason, operation_id}`.

Every pending change carries its operation (`pending_operation`: an admin rotation's
`operation_id`, or `restore:<snapshot id>`); a worker task settles only its own changes.

Rotation without an admin: every Student-visible generated account gets a new password on a
**rebuild** of a range built before, and on every **snapshot restore** (a lab reset is one;
the restore task then sets them in the guests, logging in with the passwords the snapshot
recorded). A restore or lab reset is `409` while any of the range's passwords has a change
pending. If the restore fails or is skipped, its changes go back to what the guests had.
Staff accounts keep theirs, but one an admin rotated after the snapshot was taken
is set again after the revert (the guests went back to the snapshot's password). One audit
row `range_credential_rotate` lists `rotated` and `reapplied` accounts. A snapshot records
what the guests have (a pending change's previous password); deleting the snapshot deletes
what it recorded.

Lab sessions: `GET /lab-sessions/{id}/credentials` and `POST
/lab-sessions/{id}/credentials/{credential_id}/reveal` (the session's owner only), and the
same under `/lab-access/{id}/...` with `X-Lab-Token`. A lab's implicit logins are handed to
its Student; reveals are audited with `lab_session_id` and `via: lab_session | lab_token`.

Rows are written when the range is created, when a build is accepted (which resets
`applied`; the worker sets it for what the build verified: Linux `getent passwd`, Windows a
guest login with the Administrator password) and when a lab session builds its range. A
range built from another tenant's (public) template takes none of its declared accounts,
only its own implicit logins. Stored sealed `tnsec:rk1:` (AES-256-GCM, a key derived for
stored range credentials only, bound to tenant, range and row: a stored value opens nowhere
else, not as a template password either).

Templates: a typed-in `accounts[].password` is allowed only with `scenario: true`, and never
on `role: admin`, `Administrator`, the Linux build user or `soadmin` (422). It is stored
sealed with a template seal (`tnsec:rt1:…`, bound to the template's tenant) on create and
update; `GET
/templates/{id}` shows the token to `range:credentials_read` holders of the template's
tenant and `********` to anyone else (every one of the template's passwords is masked
wherever it appears). A typed-in password for a Windows machine must meet Windows
complexity (Windows decided as the worker does), and any `tnsec:` value that is not this
tenant's template seal (a hypervisor secret, a stored range credential, another tenant's
template seal, a pre-ADR 0013 `rc1` token) is refused (422).

### `GET /range-credentials/template-issues`

**Permission: `template:update`** (an admin also sees global templates). Templates stored
before ADR 0013 with a typed-in password not on a `scenario: true` account (or on a staff
login): their ranges cannot be provisioned, scheduled bookings included, until fixed. Each
`{template_id, name, version, tenant_id, problems, upcoming_bookings}` (the caller's own
tenant's bookings only, a global template's included); never a password. The
migration logs the same list as `ACTION NEEDED`, the API logs it at start, and the Ranges page
shows it to those who can fix it.
A template change regenerates the pages of its own tenant's ranges after the response.

Wiki: a generated range page lists machines, usernames, roles and how each password was made,
and links to the range's credentials panel. It holds no password and no token.

---

### `POST /ranges/batch-provision`

Batch-provision multiple ranges: one provision operation per range, all accepted in one
transaction (one refusal accepts none). `task_id` carries the operations' ids, comma-separated.

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
| `404` | One or more range IDs not found (or another tenant's) |
| `409` | One or more ranges in non-provisionable state |

---

## Maintenance windows

ADR 0013 §7 (range certification S7). While a window covers a range, these are refused with
`409`, the window's reason and end time (UTC) in `detail`, and `Retry-After` (seconds until
it ends):

- `POST /ranges/{id}/provision`, `POST /ranges/batch-provision` (one covered range refuses
  the batch), an exercise run that provisions its range, and the scheduler clock's build;
- `POST /ranges/{id}/snapshots/{snapshot_id}/restore` and a Student lab's reset;
- `POST /ranges/{id}/destroy` (a person's normal destroy).

A new Student lab waits in its queue (`state: queued`, `error` says why) and starts when the
window ends. Never refused: a booking's teardown when it ends, a lab's end, expiry and
failed-build cleanup, and `POST /ranges/{id}/emergency-teardown`. Stop and start are not
windowed. The worker re-checks at the task's entry, and a window never leaves a range `failed`:
a build that reaches it inside a window is held (its operation's status becomes `held`, still
in flight, with `error.code = maintenance_window`, and the API re-sends it once the window ends;
a lab's build puts the range back to `created` and the lab sends it again); a restore gives its
snapshot back to `ready`. A destroy runs only when identified: a `window_bypass`, an operation
accepted before the window began, or a Student lab's cleanup; anything else is held. A range
whose work is held and on which nothing runs (every in-flight operation `held`, or a teardown
the worker held with no operation) can still be destroyed, `provisioning` or `destroying` as it
is: by emergency teardown inside the window, or a destroy after it. Bookings that overlap a
window are still made; `EventOut.maintenance_warnings` lists the windows. The clock does not
build inside a window: the booking stays `scheduled` (past its start too) and is built on the
first tick after the window ends if the session is still on (`deferred_by_window`,
`built_after_window` in the tick summary). A tenant, and the platform, may each hold at most 50
windows that have not ended (`422` beyond that).

Times must carry a zone offset (`Z` or `+hh:mm`): a time without one is `422`. They are stored
and returned in UTC; a window is `[starts_at, ends_at)`. A tenant's window covers its ranges; a
platform-wide one (`tenant_id: null`, platform administrator only) covers every tenant's.
`scope`: `all`, `ranges` (`range_ids`, the caller's own ranges; any tenant's for a platform-wide
window) or `tags` (ranges whose range template declares a matching top-level `tags:` entry).

| Method | Path | Permission |
|--------|------|------------|
| `GET` | `/maintenance-windows?include_past=false&limit=50&offset=0` | `schedule:read` |
| `GET` | `/maintenance-windows/active?range_id=` | `range:read` (the banner) |
| `GET` | `/maintenance-windows/{id}` | `schedule:read` |
| `POST` | `/maintenance-windows` | `range:admin`; `platform_wide` also needs a platform administrator |
| `PUT` | `/maintenance-windows/{id}` | as `POST`; `platform_wide` cannot change |
| `DELETE` | `/maintenance-windows/{id}` | as `POST` |
| `POST` | `/ranges/{id}/emergency-teardown` | `range:admin` |

Another tenant's window is `404`; a platform-wide one is `403` to change for anyone but a
platform administrator. Every write is audited (`resource_type: maintenance_window`).

**`POST /maintenance-windows`**
```json
{
  "reason": "vCenter 8.0U3 upgrade",
  "starts_at": "2026-11-01T22:00:00-04:00",
  "ends_at": "2026-11-02T02:00:00-05:00",
  "scope": "ranges",
  "range_ids": ["f6a7b8c9-d0e1-2345-f012-456789012345"],
  "tags": [],
  "platform_wide": false
}
```

**Response `201 Created`:**
```json
{
  "id": "3b0d…", "tenant_id": "00000000-0000-0000-0000-000000000001", "platform_wide": false,
  "reason": "vCenter 8.0U3 upgrade",
  "starts_at": "2026-11-02T02:00:00Z", "ends_at": "2026-11-02T07:00:00Z",
  "scope": "ranges", "range_ids": ["f6a7b8c9-d0e1-2345-f012-456789012345"], "tags": [],
  "active": false, "can_edit": true, "created_by": "…", "created_at": "…", "updated_at": "…"
}
```

**A refusal inside it (`409`, `Retry-After: 16200`):**
```json
{"detail": "Provisioning is refused during a maintenance window until 2026-11-02T07:00:00+00:00 (2026-11-02 02:00 to 2026-11-02 07:00 UTC): vCenter 8.0U3 upgrade. Automated cleanup still runs; an administrator can tear a range down in an emergency (range:admin, with a reason)."}
```

### `POST /ranges/{range_id}/emergency-teardown`

**Permission: `range:admin` (admin only). Dangerous.** A destroy that a maintenance window does
not refuse. Body: `{"confirm_range_id": "<range id again>", "reason": "<10-500 characters, at least 10
besides spaces>"}`.
`202` with the range (`destroying`) and an `Operation-Id` header; the operation's
`window_bypass` is `emergency`. Audited as `emergency_teardown` with the reason and the window
it overrode, and written to the range's newest certification record as an `overrides` entry
(kind `override`, action `emergency_teardown`, the operation and window ids). Otherwise a destroy: `409` in a state that cannot be destroyed, or for a Student lab's
range (end the lab session instead; that always runs); `422` when `confirm_range_id` differs
or the reason is missing or short; `404` for another tenant's range.

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

Judge an objective achieved (ADR 0020: instructors judge team exercises; nothing else
achieves an exercise objective). Records who judged it, the instructor's note and the
Student detections the judgement relied on, timestamps it, and re-totals the exercise's
score. The exercise must be in the caller's tenant and running or paused.

**Permission: `objective:ack`** (instructor, admin)

**Request (JSON body, optional):**
```json
{
  "evidence": "Blue cell found the beacon at 12:05",
  "cites": ["5b2f0a8e-1c3d-4e5f-8a9b-0c1d2e3f4a5b"]
}
```
`cites` are detection submissions (`POST .../detections`) on this objective in this run;
any other id is `422`. A cited submission becomes `verdict: "cited"`.

**Response `200 OK`:** the objective, with `evidence` the judgement as JSON:
```json
{
  "ref_id": "obj-1",
  "achieved": true,
  "evidence": "{\"source\": \"instructor\", \"by\": \"Sgt Rivera\", \"by_id\": \"...\", \"note\": \"Blue cell found the beacon at 12:05\", \"cites\": [\"5b2f...\"], \"students\": [\"...\"]}",
  "achieved_at": "2026-01-15T10:45:00Z",
  "points": 25
}
```

`409` if the exercise is not running/paused or the objective is already achieved.

### `DELETE /exercises/{exercise_id}/objectives/{ref_id}/ack`

Withdraw a judgement while the exercise is running or paused. Its cited submissions go back
to `recorded`, and the score is re-totalled. **Permission: `objective:ack`**. `409` if the
exercise is closed or the objective is not achieved.

| Status | Condition |
|--------|-----------|
| `200` | Objective acknowledged |
| `403` | Caller lacks `objective:ack` (Student, observer, range_ops) |
| `404` | Exercise not in caller's tenant, or objective not found |
| `409` | Exercise not running/paused, or objective already achieved |

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

Ingest telemetry events into the range's index (`range-<range_id>`). **Permission:
`telemetry:write`** (Students cannot write; another tenant's range is 404). The body is a
**JSON array** of events. Each event is tagged with `range_id`, `tenant_id`, a
`@timestamp` (now, if absent) and a `mitre_technique` list when one is known.

**Request:**
```json
[
  {
    "timestamp": "2026-01-15T10:30:00Z",
    "event.action": "process_create",
    "process.name": "powershell.exe",
    "process.command_line": "powershell -enc ...",
    "host.name": "ws01",
    "source": "sysmon"
  }
]
```

**Response `202 Accepted`:**
```json
{
  "accepted": 1
}
```

---

### `GET /telemetry/{range_id}/search?q=*&size=50`

Search telemetry events for a range. `q` is a small closed grammar
(`app/search_backends/query.py`), never OpenSearch `query_string`: `field:value`,
`field:"a phrase"`, `field:prefix*`, `field:*` (exists) and free text, ANDed — no regex,
fuzzy, leading wildcards or `_`-prefixed fields; a query outside it is `422`. Platform
label fields are not searchable for non-staff.

**Permission:** staff with `telemetry:read` read their tenant's ranges; a Student reads
only their own lab session's range or a running exercise they take part in
(`app/telemetry_access.py`).

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | `*` | Query (grammar above) |
| `size` | int | 50 | Max results (1–500) |

**Response `200 OK`:** the raw OpenSearch response (newest first):
```json
{
  "hits": {
    "total": {"value": 1250},
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
}
```

---

## Audit Log

### `GET /audit-log`

Retrieve audit log entries. **Permission: `audit:read`**

**Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Max results (max 500) |
| `offset` | int | 0 | Pagination offset |
| `tenant_id` | uuid | -- | Filter by tenant (**platform admin only**) |

There are no `resource_type` / `action` filter parameters (`routers/admin.py`).

**Response `200 OK`:**
```json
[
  {
    "id": "d0e1f2a3-b4c5-6789-3456-890123456789",
    "timestamp": "2026-01-15T10:30:00Z",
    "user_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
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

**Authentication:** every route except `GET /health` requires the service token —
`Authorization: Bearer <AI_SERVICE_TOKEN>` or `X-Service-Token: <AI_SERVICE_TOKEN>` — checked
constant-time (`ai-orchestrator/app/main.py`). Without `AI_SERVICE_TOKEN` set the service refuses
to serve when `TN_ENV=production`.

All four routes return the same **`GenerateResponse`** envelope:

```json
{
  "task": "detection-rule",
  "model_used": "llama3.1:70b-instruct-q5_K_M",
  "node_used": "wile",
  "backend": "ollama",
  "output": "...",
  "usage": {"prompt_tokens": 15, "completion_tokens": 120},
  "cached": false,
  "latency_ms": 812.4
}
```

### `POST /ai/generate`

General-purpose text generation.

**Request:**
```json
{
  "task": "general",
  "prompt": "Explain the MITRE ATT&CK technique T1566.001",
  "system_prompt": "",
  "context": {},
  "model": "",
  "max_tokens": 2000
}
```

---

### `POST /ai/detection-rule`

Generate a detection rule (`sigma`, `opensearch` or `suricata`) for an ATT&CK technique.

**Request:**
```json
{
  "technique": "T1059.001",
  "data_source": "sysmon",
  "format": "sigma"
}
```

The generated rule text is in `output` of the `GenerateResponse` — there is no separate `rule` key.

---

### `POST /ai/scenario-suggest`

Design a scenario from training objectives.

**Request:**
```json
{
  "objectives": ["Identify lateral movement via PsExec"],
  "difficulty": "advanced",
  "duration_minutes": 120
}
```

The scenario YAML is in `output` — there is no `suggestions` key.

---

### `POST /ai/aar-analysis`

Analyze After-Action Report data and generate narrative insights.

**Request:**
```json
{
  "report_data": "{\"scores\": {\"total\": 75, \"max\": 100}, ...}",
  "context": {}
}
```

The analysis is in `output` — there are no `analysis`/`recommendations` keys.

---

## WebSocket Protocol

TrueNorth Range provides real-time updates via WebSocket connections. The WebSocket manager supports up to **10,000 concurrent connections** with Redis pub/sub for multi-instance broadcasting.

### Connection

The channel is part of the URL and the access token is the second **subprotocol**
(`app/main.py`: `@app.websocket("/ws/{channel}")`, `app/ws_auth.py`):

```js
const ws = new WebSocket("ws://localhost:4200/ws/range.<range_id>", ["bearer", token]);
// direct to the API container in dev: ws://127.0.0.1:8081/ws/<channel>
```

In development the web nginx upgrades only `/ws/` (its `/api/` location sends no
`Upgrade` header — `control-plane/web/nginx.conf`), so the `:4200` URL above has no
`/api` prefix. In production the edge strips `/api/` and upgrades
(`infra/platform/nginx/conf.d/truenorth.conf`), so `wss://<site>/api/ws/<channel>`
works there.

A missing/invalid token, or a channel the user may not open, is closed with **1008**.

**Channels:** `range.<id>`, `exercise.<id>`, `exercise.<id>.staff` (as `exercise.<id>`,
for holders of `scenario:update` — full instructor-inject events go there),
`tenant.<id>` (the user's tenant) and `system.*` (platform administrator only).
Collaboration rooms are `room.<id>`, joined in-band rather than in the URL.

### Message Format

All server messages use a structured JSON envelope
(`websocket_manager.py:_build_message`):

```json
{
  "type": "room_chat",
  "channel": "room.f6a7b8c9",
  "sequence": 42,
  "timestamp": "2026-01-15T10:30:00.123456+00:00",
  "data": {
    "sender_user_id": "b2c3d4e5-...",
    "text": "hello"
  }
}
```

`data` is the sending client's payload; the server stamps only `sender_user_id`
(`app/main.py:707`) — a client cannot speak for another member.

### Message Types

| Type | Description | Data |
|------|-------------|------|
| `room_member_joined` | A user joined a collaboration room | `room_id`, `user_id`, `display_name`, `conn_id` |
| `room_member_left` | A user left a collaboration room | `room_id`, `user_id`, `conn_id` |
| `room_chat` | A chat message from another room member | `sender_user_id` plus the sender's payload |
| `room_cursor` | Another member's cursor position | `sender_user_id` plus the sender's payload |
| `room_members` | Reply to a `room_members` action | `members` list |
| `annotation_created` | An analyst annotation was created (`POST /ops/exercises/{id}/annotations`) | `AnnotationOut` fields (`app/routers/ops_center.py`) |
| `command_shared` | A command was shared (`POST /ops/exercises/{id}/commands`) | `SharedCommandOut` fields (`app/routers/ops_center.py`) |
| `instructor_inject` | An instructor inject fired | On `exercise.<id>.staff` the full event; on `exercise.<id>` that it happened and, for a narrative inject, its text (`app/ws_auth.py:participant_inject_event`) |
| `ping` / `pong` | Heartbeat | — |
| `ack` / `error` | Reply frames (ack for unrecognised input, error details) | `data` / `detail` |

`annotation_created`, `command_shared` and `instructor_inject` are broadcast on the
exercise's `exercise.<id>` (and `exercise.<id>.staff`) channels today
(`app/routers/ops_center.py`). Other domain events (`range.ready`, `exercise.started`,
`scenario.inject.fired`, `system.alert`, … — dotted names in `app/events.py`) are
published on the internal event bus; the WebSocket endpoint is not a live feed of those.

### Channel Subscription

The channel is fixed at connect time — there is no `subscribe` message. Once
connected, a client collaborates in rooms with these actions:

```json
{"action": "join_room", "room_id": "a7b8c9d0", "display_name": "Alice"}
{"action": "room_message", "room_id": "a7b8c9d0", "type": "room_chat", "data": {"text": "hello"}}
{"action": "room_members", "room_id": "a7b8c9d0"}
{"action": "leave_room", "room_id": "a7b8c9d0"}
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

| Format | Development (API direct, loopback only) | Production (edge, management networks only) |
|--------|------------------------------------------|---------------------------------------------|
| Interactive (Swagger UI) | `http://127.0.0.1:8081/docs` | `https://<site>/api/docs` |
| ReDoc | `http://127.0.0.1:8081/redoc` | `https://<site>/api/redoc` |
| Raw JSON | `http://127.0.0.1:8081/openapi.json` | `https://<site>/api/openapi.json` |

The web container's nginx on `:4200` answers these with **404** in development —
operator surfaces are not served through it (`control-plane/web/nginx.conf`, "Operator
surfaces are not served through the web container"). In production the edge serves
`/api/docs`, `/api/redoc` and `/api/openapi.json` restricted to management networks
(`infra/platform/nginx/conf.d/truenorth.conf`). The published contract copy lives at
`docs/interfaces/openapi.json`.

---

## Endpoint Summary (core)

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
| 9 | `POST` | `/teams` | `user:update` or `team:manage_own` | Create team |
| 10 | `GET` | `/teams` | `user:read` | List teams |
| 11 | `DELETE` | `/teams/{id}` | `user:update` or `team:manage_own` | Delete team |
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
| 30 | `POST` | `/ranges/{id}/stop` | `range:provision` | Stop range (power off) |
| 30a | `POST` | `/ranges/{id}/start` | `range:provision` | Start range (power on) |
| 31 | `POST` | `/ranges/batch-provision` | `range:batch_provision` | Batch provision |
| 31a | `POST` | `/ranges/{id}/lease/force-release` | `range:lease_force_release` | Force-release an abandoned operation's lease tombstone (admin) |
| 31b | `GET` | `/ranges/{id}/credentials` | `range:read` + role rule | Machine accounts, no passwords: staff all (`range:credentials_read`), Students theirs, observers 403 |
| 31c | `POST` | `/ranges/{id}/credentials/{cred_id}/reveal` | `range:read` + role rule | One password; audited (`range_credential_reveal`) |
| 31d | `POST` | `/ranges/{id}/credentials/{cred_id}/rotate` | `range:credentials_rotate` | A new generated password; audited (`range_credential_rotate`) |
| 31e | `GET` | `/range-credentials/template-issues` | `template:update` | Templates whose typed-in passwords ADR 0013 refuses |
| 31f | `POST` | `/ranges/{id}/credentials/{cred_id}/settle` | `range:credentials_rotate` | A stuck pending change back to the in-guest password; audited (`range_credential_settle`) |
| 31g | `POST` | `/ranges/{id}/emergency-teardown` | `range:admin` | Tear a range down inside a maintenance window, with a reason (admin) |
| 31h | `GET/POST/PUT/DELETE` | `/maintenance-windows` | `schedule:read` / `range:admin` | Maintenance windows (ADR 0013 §7) |
| 32 | `POST` | `/exercises` | `exercise:create` | Create exercise |
| 33 | `GET` | `/exercises` | `exercise:read` | List exercises |
| 34 | `GET` | `/exercises/{id}` | `exercise:read` | Get exercise |
| 35 | `POST` | `/exercises/{id}/start` | `exercise:start` | Start exercise |
| 36 | `POST` | `/exercises/{id}/pause` | `exercise:pause` | Pause exercise |
| 37 | `POST` | `/exercises/{id}/complete` | `exercise:complete` | Complete exercise |
| 38 | `GET` | `/exercises/{id}/objectives` | `exercise:read` | List objectives |
| 39 | `POST` | `/exercises/{id}/objectives/{ref}/ack` | `objective:ack` | Ack objective (instructor) |
| 39a | `DELETE` | `/exercises/{id}/objectives/{ref}/ack` | `objective:ack` | Withdraw the ack (409 if exercise closed or not achieved) |
| 40 | `POST` | `/exercises/{id}/aar/generate` | `aar:generate` | Generate AAR |
| 41 | `GET` | `/exercises/{id}/aar` | `aar:read` | Get AAR JSON |
| 42 | `GET` | `/exercises/{id}/aar/html` | `aar:read` | Get AAR HTML |
| 43 | `POST` | `/telemetry/{range_id}/events` | `telemetry:write` | Ingest events (202) |
| 44 | `GET` | `/telemetry/{range_id}/search` | `telemetry:read` | Search events (Students: own lab/participating exercise) |
| 45 | `GET` | `/audit-log` | `audit:read` | Audit log |
| 46 | `POST` | `/ai/generate` | Service token | AI text gen |
| 47 | `POST` | `/ai/detection-rule` | Service token | Generate detection |
| 48 | `POST` | `/ai/scenario-suggest` | Service token | Scenario suggest |
| 49 | `POST` | `/ai/aar-analysis` | Service token | AAR analysis |
| -- | `WS` | `/ws/{channel}` | Authenticated | WebSocket (token as subprotocol) |