# TrueNorth — Current State

**2026-10-08, measured on `github/main` (`f2cf12b`) + #90 (`claude/carve-security`).**
This replaces the 2026-08-18 reconstruction. That document's defects are fixed: the
duplicate Alembic tree is gone, the dead injector tree is gone, ranges are tenant-scoped,
and CI is real. Trust order is unchanged: code > configuration > running services >
tests > docs. Labels: `IMPLEMENTED` `PARTIAL` `UNTESTED` `PLANNED`.

## Platform

| Area | State | Evidence |
|---|---|---|
| API contract | `IMPLEMENTED`: 335 paths | `docs/interfaces/openapi.json`, gate-checked against the code (ADR 0002); the built API image serves the same 335 |
| Section modules | `IMPLEMENTED` (stage-4 base) | `control-plane/api/app/<module>/`: scheduler, capacity, range_leases, range_ops, lab_sessions, network_inventory, noise, greyspace, detections, scenario_runs, course_releases, course_publishing, lms, notifications; wiki and tickets |
| Adapters | `IMPLEMENTED` (ADR 0001) | `*_backends/` (ai, auth, console, hypervisor, moodle, search, threat_intel, vector); worker `provisioners/` (vsphere_api, vsphere_guest, vsphere_infra, mock, terraform, hyperv, proxmox_api) |
| Provisioning target | vSphere (`vsphere_api`); Proxmox is kept passing only | vcsim lane in CI; live lab nightly (`lab.yml`), which needs the self-hosted runner |
| Migrations | one Alembic tree, `control-plane/api/alembic/` | CI test-python runs the real chain on PostgreSQL 16 and fails if any PostgreSQL test skips |
| Worker ↔ API | typed task contract and table mirror, drift-checked by the gate | `worker/contracts.py`, `worker/tables.py`; MOSA ratchet `.dod-mosa-baseline` |
| Web | Angular, generated API client | `npm run check:api` in CI; Karma and Playwright lanes |
| Detection credit | Students earn credit by submitting detections (ADR 0005) | `tests/worker/test_detection_postgres.py` on PostgreSQL in CI |
| QSP / CFITES spine | `IMPLEMENTED` | `crosswalk.csv` mapped to NICE `SP800-181r1`; the four Red Analyst rows carry `DCWF-TODO` (test-asserted); DP3-5 rank labels left blank on purpose |
| Content | `PARTIAL`: the binding constraint | scenario content and golden images lag the platform; see `docs/content-library.md` |
| LMS / Moodle | publication to a per-tenant Moodle (ADR 0004); cmi5 is packaging only at stage 4 | CI `moodle` lane; `docs/cmi5-packaging-decision.md` |
| Security | sealed credentials at rest (`TN_SECRETS_KEY`), non-root images (#90); pip-audit and npm audit blocking; image Trivy gate | `ci.yml` supply-chain and build-docker; `release.yml` |

## Release and deployment: `PARTIAL`

Tags `v*` publish five images by digest, gated by Trivy (fixable HIGH/CRITICAL), with
SBOMs and `release-manifest.json` (`docs/release.md`). **Not done:** the installer does
not consume the manifest yet (`compose.prod.yml` still builds on the target), and the
API reports a hard-coded `APP_VERSION = "0.1.0"` instead of `TN_VERSION`. No workflow
deploys. The old deploy workflows were deleted because they could not work.

## Tests

The gate (`scripts/dod.sh`, the same selection as CI test-python) and the CI lanes are
listed in `RESUME.md`. Measured 2026-10-08 on macOS with PostgreSQL 16 and without
OpenSearch: **3758 passed, 34 skipped, 0 failed**. The skips are 29 OpenSearch tests
(they need `TEST_OPENSEARCH_URL`; CI sets it), 4 pfSense `php` checks (php CLI absent)
and 1 cmi5 XSD that is not vendored. Without PostgreSQL the PostgreSQL tests also skip
locally; CI fails on that. Integration, e2e, Moodle, vcsim and Greyspace evidence comes
from CI runs, not from this count.

## Open

1. Installer: install by digest from `release-manifest.json`.
2. API: read `TN_VERSION`.
3. k6 smoke non-blocking; the load scripts predate the API (`tests/load/README.md`).
4. Live vSphere lab evidence pending (credentials, self-hosted runner).
5. Content authoring remains the binding constraint (about 2,600 build hours).
