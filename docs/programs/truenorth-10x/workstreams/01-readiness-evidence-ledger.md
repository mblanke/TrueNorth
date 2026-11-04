# Workstream 01 — readiness evidence ledger

Status: proposed; begins after 00. Suggested branch for the first slice:
`claude/10x-01-readiness-evidence`.

## Outcome

Turn existing class, exercise and competency outcomes into temporal, provenance-linked facts
that can support person/team/unit readiness views without inventing a universal score.

## Current foundations

- `control-plane/api/app/classes/models.py`: tenant-owned `ClassResult` stores pass, fail or
  incomplete, attendance snapshot, deciding actor/time and no numeric grade.
- `control-plane/api/app/classes/readiness.py` and `readiness_router.py`: read-only unit report.
- `control-plane/api/app/models.py`: competency assertions and auto-assessments.
- `control-plane/web/src/app/features/readiness/`: current commander-facing projection.

## Contract PR before implementation

Write an ADR defining evidence types, subject references, source/version identity, observed
and expiry times, supersession/retraction, actor/scorer/policy versions, tenant ownership,
classification/handling field semantics, integrity field and missing-evidence behavior.

Decisions D01, D02 and D04 remain human gates. The contract may support markings and
pseudonymous subject references but must not invent CAF policy. The ledger payload must never
contain display name, email or numeric grade in the first Unclassified slice.

## First vertical slice

1. Add an append-only readiness evidence event model and linear Alembic migration. Candidate
   fields: UUID, tenant, subject UUID/token, evidence type, source type/id/revision,
   pass/fail/incomplete value, observed/valid-until/recorded times, actor, policy/scorer
   version, supersedes/retracts reference, minimal JSON metadata and integrity hash.
2. On class close-out, idempotently record a `class.result.recorded` evidence event for each
   result. On reopen/correction, append an explicit superseding/retraction event; do not mutate
   historical evidence into a different event.
3. Add a tenant- and permission-scoped evidence-detail API and source link from the existing
   readiness projection. Preserve the current “never a score” behavior.
4. Show source, observed time, validity/unknown state and human-vs-calculated provenance in the
   readiness UI. Do not add a percentage tile.
5. Keep CSV/PDF behavior stable unless the accepted contract explicitly versions the export.

Likely files: class close-out service/router/model tests, a new bounded readiness-evidence
module, model import registry, Alembic revision, OpenAPI snapshot/generated Angular schema,
readiness API service/component/specs and relevant tenant/RBAC tests. Recheck current paths.

## Required tests

- Same close-out retry creates one logical fact.
- Reopen/correction preserves prior fact and appends provenance.
- Foreign-tenant and unauthorized evidence lookup are refused without existence leakage.
- Missing/expired/retracted evidence does not become a pass.
- No display identity or numeric grade appears in the ledger payload or exchange fixture.
- Fresh install and upgrade reach one Alembic head; downgrade/recovery behavior is documented.
- API contract generation and Angular tests pass.

## Subsequent slices

Project exercise/scenario evidence; map facts to PO/EO/competency and approved mission tasks;
add team/unit projections; implement expiry/remediation/retest; add an official-record adapter
only after D01/D02/D04 are approved.

## Rollout and recovery

Ship write + shadow-read first, backfill only with an explicit idempotent command, compare the
old report to the projection, then enable evidence drill-down. Do not delete `class_results`.
Disable the projection flag and retain append-only rows if rollback is required.

## Skills and ownership

Primary: `tn-curriculum-traceability`; add `tn-api-contracts`, `tn-schema-migrations`,
`tn-angular-delivery` and `tn-tenant-auth` for their layers. The integrator owns the schema and
migration token. A separate reviewer uses `tn-change-review`.
