# TrueNorth — Current State

**2026-10-08, measured on `github/main` (`6d7b530`: #90, #97, #99 and #101 merged);
status lines updated at `83d7d43` (#107); release status updated 2026-10-09 at
`e015d27` (`v1.0.0-rc3`, #117).** All 17 modules are at stage 4. `v1.0.0-rc3` is the
latest signed pre-release and runs on staging; `v1.0.0` is not tagged
(`CHANGELOG.md`; limitations and staging record in `docs/release-notes/v1.0.0.md`).
This replaces the 2026-08-18 reconstruction. That document's defects are fixed: the
duplicate Alembic tree is gone, the dead injector tree is gone, ranges are tenant-scoped,
and CI is real. Trust order is unchanged: code > configuration > running services >
tests > docs. Labels: `IMPLEMENTED` `PARTIAL` `UNTESTED` `PLANNED`.

## Platform

| Area | State | Evidence |
|---|---|---|
| API contract | `IMPLEMENTED`: 336 paths | `docs/interfaces/openapi.json`, gate-checked against the code (ADR 0002); the built API image serves the same 336 |
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

Tags `v*` on `main` publish five images by digest, gated by Trivy (fixable
HIGH/CRITICAL), with SBOMs and `release-manifest.json`, all cosign-signed
(`docs/release.md`). The installer verifies the manifest signature and installs by
digest (`compose.prod.yml` has no `build:`; #109, #111). The API reports `TN_VERSION`
(#103). No workflow deploys. The old deploy workflows were deleted because they could not work.
Staging (`tn-staging`, Ubuntu 24.04, no vCenter, no AD): clean install of rc2 from the
signed release and upgrade rc2 → rc3, as reported by the staging session and recorded
in `docs/release-notes/v1.0.0.md`. **Not done:** a run against real vCenter; Helm is
exercised only by the CI kind smoke install.

## Tests

The gate (`scripts/dod.sh`, the same selection as CI test-python) and the CI lanes are
listed in `RESUME.md`. Measured 2026-10-08 on macOS with PostgreSQL 16 and without
OpenSearch: **3875 passed, 34 skipped, 0 failed**. Karma: 503 of 503. The skips are 29 OpenSearch tests
(they need `TEST_OPENSEARCH_URL`; CI sets it), 4 pfSense `php` checks (php CLI absent)
and 1 cmi5 XSD that is not vendored. Without PostgreSQL the PostgreSQL tests also skip
locally; CI fails on that. Integration, e2e, Moodle, vcsim and Greyspace evidence comes
from CI runs, not from this count.

## Open

1. v1.0.0 known limitations: `docs/release-notes/v1.0.0.md` (and #118, the nightly
   backup wrapper on a new host, open).
2. Live vSphere lab evidence pending (credentials, self-hosted runner).
3. Content authoring remains the binding constraint (about 2,600 build hours).
