# ADR 0002 — Key interfaces are published, versioned and checked

- Status: accepted
- Date: 2026-10-03
- MOSA pillar: designated key interfaces, open standards, conformance

## Context
The API contract existed only at runtime (FastAPI's generated schema). The Angular
client and `core/models/index.ts` were hand-maintained copies of `schemas.py`, and API
changes were invisible in review. Celery tasks are dispatched by string name with
untyped arguments.

## Decision
Key interfaces, and where each one's contract lives:

| Interface | Contract artifact | Check |
|---|---|---|
| Control-plane HTTP API | `docs/interfaces/openapi.json` (OpenAPI 3.1, generated) | `scripts/export_openapi.py --check` in `dod.sh` and CI |
| Scenario / template content | `scenario-engine/schemas/*.schema.json` (JSON Schema) | content validation tests |
| API → worker tasks | Pydantic payload models (shared contracts package) | contract tests |
| Learning records | xAPI 1.0.3 statements, LTI 1.3 | unit + schema tests |
| Identity | OIDC (any compliant IdP) | `auth_backends` |

Rules:
1. The committed `openapi.json` must equal what the code produces. An intended API
   change regenerates it (`python scripts/export_openapi.py`) in the same commit.
2. Every operation has a unique, stable `operationId`. Do not register one handler for
   several methods with `api_route(methods=[...])`; give each method its own decorator
   and `operation_id`.
3. The HTTP API is versioned under `/api/v1` (`app/versioning.py`). Breaking changes go
   to a new version; the old one gets RFC 8594 `Deprecation`/`Sunset` headers.
4. Clients are generated from the contract, not hand-written.

## Consequences
Any API shape change is a visible diff in `openapi.json`, reviewable by humans and
usable by third parties without reading the code.
