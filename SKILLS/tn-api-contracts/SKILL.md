---
name: tn-api-contracts
description: "Create or change FastAPI endpoints and their Angular/worker contracts in TrueNorth; use for request schemas, responses, and API behavior."
---

# API contract delivery

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/app/routers`
- `control-plane/api/app/schemas.py`
- `control-plane/api/app/tenancy.py`
- `control-plane/api/app/rbac.py`
- `control-plane/web/src/app/core/services/api.service.ts`

## Workflow

1. Identify the actual router, schema, callers, permissions, and persisted model. Verify mounted route paths in main.py rather than relying on API documentation.
2. Define success, invalid input, missing resource, foreign tenant, and state conflict behavior. Keep public compatibility or document a migration for every consumer.
3. Scope related object lookups as well as the primary object. Shared catalogue reads do not imply shared write permission.
4. Keep database changes and queued work consistent. Do not report a job as running merely because its DB state was committed before dispatch.
5. Bound list queries and return totals separately when clients require them. Assess query counts for hierarchy and report endpoints.
6. Prefer thin routes with explicit service boundaries when extracting substantial business behavior.

## Verification

Exercise the changed contract through API tests including authorization and failure cases; check the frontend caller and worker payload where the contract crosses layers.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Endpoint implementation with request/response examples, compatibility notes, and behavior-focused tests.

## Boundary

Do not weaken an existing authorization boundary to simplify client integration.

