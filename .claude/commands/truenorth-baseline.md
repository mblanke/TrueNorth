---
description: Reconstruct what TrueNorth actually contains today from the working tree, not from stale docs. Writes docs/current-state.md.
---

# /truenorth-baseline

Reconstruct the platform's real current state. The documentation in `docs/` has drifted
from the code — treat it as the least reliable source, not the most.

## Order of trust (highest first)

1. Actual source code
2. Current configuration (`.env*`, `infra/platform/docker/compose*.yml`, `alembic/versions/`)
3. Currently running services (`docker ps`, listening ports, container env)
4. Uncommitted files and `git diff`
5. Tests — and whether they actually pass, not merely exist
6. Documentation

Never conclude a capability is missing because the docs omit it, or present because the
docs describe it.

## Classify every area

Use exactly these labels:

`IMPLEMENTED` · `PARTIAL` · `BROKEN` · `UNTESTED` · `UNDOCUMENTED` · `PLANNED` ·
`STALE-DOC` · `DUPLICATED` · `DEPRECATED`

## Areas to cover

control-plane API · Angular frontend · worker/Celery · scenario engine · injectors ·
validators · range lifecycle · Terraform/Packer · Keycloak/RBAC · Redis · OpenSearch ·
MinIO · WebSockets · xAPI/cmi5 · AAR · PDF reporting · AI orchestrator · Velociraptor ·
HELK · content library · QSP/CFITES curriculum spine · testing · CI/CD · security ·
multi-tenancy · observability

## Rules

- **Cite evidence per claim** — a file path, a route, a passing test, a running
  container. A classification with no evidence is a guess; mark it `UNTESTED` instead.
- **Prefer running the thing** over reading about it. `pytest -q` beats a coverage badge.
- Record deliberate configuration, not just gaps. `PROVISIONER_BACKEND=mock` on this
  host is intended — provisioning happens on a separate demo environment. Do not file
  intended configuration as `BROKEN`.
- Note where two subsystems solve the same problem (`DUPLICATED`).

## Output

Write `docs/current-state.md`. Do **not** overwrite any existing architecture doc —
this report is reviewed first, and only then used to correct them.
