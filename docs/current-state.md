# TrueNorth — Current State

**Header and the "Classes and calendars" row refreshed 2026-10-27 on `github/main`
(`fd4f79a6`, through #312); the other rows are from 2026-10-11 (`ce254a18`, #142).** The
newest tag is `v1.2.0-rc15` (2026-10-22). It predates all of ADR 0014, so no release
candidate has the classes work yet. `v1.2.0` is not tagged (`v1.1.0` was released
2026-10-09). How each role uses the product: `docs/guides/03-admin-first-day.md`,
`04-instructor-guide.md`, `05-student-quick-start.md`.
Changes: `CHANGELOG.md`; limitations, simulator-only evidence and the staging record:
`docs/release-notes/v1.2.0.md` (v1.0.0's: `docs/release-notes/v1.0.0.md`).
This replaces the 2026-08-18 reconstruction. That document's defects are fixed: the
duplicate Alembic tree is gone, the dead injector tree is gone, ranges are tenant-scoped,
and CI is real. Trust order is unchanged: code > configuration > running services >
tests > docs. Labels: `IMPLEMENTED` `PARTIAL` `UNTESTED` `PLANNED`.

## Platform

| Area | State | Evidence |
|---|---|---|
| API contract | `IMPLEMENTED`: 357 paths | `docs/interfaces/openapi.json`, gate-checked against the code (ADR 0002) |
| Section modules | `IMPLEMENTED` (stage-4 base) | `control-plane/api/app/<module>/`: scheduler, capacity, range_leases, range_ops, lab_sessions, network_inventory, noise, greyspace, detections, scenario_runs, course_releases, course_publishing, lms, notifications; wiki and tickets |
| Adapters | `IMPLEMENTED` (ADR 0001) | `*_backends/` (ai, auth, console, hypervisor, moodle, search, threat_intel, vector); worker `provisioners/` (vsphere_api, vsphere_guest, vsphere_infra, vsphere_roles, mock; hyperv and proxmox_api only with `EXPERIMENTAL_PROVISIONERS`; terraform removed in #131) |
| Provisioning target | vSphere (`vsphere_api`) with per-tenant vCenter credentials for every operation (H6, fixed in #131); Proxmox is kept passing only | **simulator-proven only**: vcsim lane in CI; live lab `lab.yml` is manual-only (nightly off) until the self-hosted runner is registered |
| Greyspace | `IMPLEMENTED` on vSphere (gs-core VM, `configure_range`, #133) | **simulator-proven only**: vcsim and the T0 Docker stack (CI `greyspace`) |
| Migrations | one Alembic tree, `control-plane/api/alembic/` | CI test-python runs the real chain on PostgreSQL 16 and fails if any PostgreSQL test skips |
| Worker ↔ API | typed task contract and table mirror, drift-checked by the gate | `worker/contracts.py`, `worker/tables.py`; MOSA ratchet `.dod-mosa-baseline` |
| Web | Angular, generated API client | `npm run check:api` in CI; Karma and Playwright lanes |
| Detection credit | Students earn credit by submitting detections (ADR 0005) | `tests/worker/test_detection_postgres.py` on PostgreSQL in CI |
| QSP / CFITES spine | `IMPLEMENTED` | `crosswalk.csv` mapped to NICE `SP800-181r1`; the four Red Analyst rows carry `DCWF-TODO` (test-asserted); DP3-5 rank labels left blank on purpose |
| Content | `PARTIAL`: the binding constraint | 44 catalogue courses, all drafts; staging's 45 courses are ARC² TEST CONTENT (auto-accepted); C105's release is a CI/CATAPULT fixture and stays a draft; see `docs/content-library.md` |
| LMS / Moodle | publication to a per-tenant Moodle (ADR 0004), completions and grades back (#132); TrueNorth is a cmi5 LMS with conformant xAPI (#139), Moodle launches its AUs over LTI 1.3 (#140) | CI `moodle` and `cmi5` (ADL CATAPULT) lanes; `docs/cmi5.md` |
| Monitoring | Prometheus rules match `docs/operations.md`; OpenSearch health, Moodle, ARC² runner and backup freshness come from host textfile metrics | `alerts.yml` + promtool unit tests (`tests/contracts/test_prometheus_rules.py`); alert delivery not exercised on staging |
| Backups | PostgreSQL, MinIO, OpenSearch snapshot, Moodle files, ARC² runs, escrow | `scripts/backup/`, CI `backup-drill`; `docs/runbooks/backup-restore.md` |
| Security | sealed credentials at rest (`TN_SECRETS_KEY`), non-root images (#90); pip-audit and npm audit blocking; image Trivy gate. `PARTIAL` against NIST 800-53: no KMS or disk/database encryption, internal traffic plain; login banner, privileged-user MFA and audit export/retention are being built for v1.2.0 | `ci.yml` supply-chain and build-docker; `release.yml`; control-by-control table in `docs/security.md` ("Compliance Mapping", 2026-10-12) |
| Scenario injects | `PARTIAL`: every injector writes synthetic telemetry; only `greyspace_breadcrumb` acts on a range (vSphere, simulator-proven). No sensor is installed on range VMs | `docs/scenario-inject-execution.md` |
| Teams | `PARTIAL`: team CRUD (admin UI) and a membership API; a class's collective sessions get balanced teams (#299); no team scoring or leaderboards (planned, R2) | `control-plane/api/app/routers/admin.py`, `app/classes/teams.py`; `docs/user-guide.md` "Not in v1.2.0" |
| Classes and calendars (ADR 0014) | `IMPLEMENTED` on main, untagged. Delivered: classes and roll call, Rules of use, seat forfeit, booking approval, per-session ranges and teams, attendance and close-out, My classes, Events and Content calendars, content clock and feeds, AD status sync, self-serve lab gate, app email. Unit readiness #315 is in review. Several settings have no screen yet (roll-call domains, a course's default mode, class edit and hand-over, review cycles and themes, trust keys); see the guides' "Known gaps" | `control-plane/api/app/classes/`, `acknowledgement/`, `content_review/`, `content_feeds/`, `ad_status_sync/`, `lab_sessions/capacity_gate.py`; `docs/adr/0014-classes-onboarding-and-calendar.md` |

## Release and deployment: `PARTIAL`

Tags `v*` on `main` publish five images by digest, gated by Trivy (fixable
HIGH/CRITICAL), with SBOMs and `release-manifest.json`, all cosign-signed
(`docs/release.md`). The installer verifies the manifest signature and installs by
digest (`compose.prod.yml` has no `build:`; #109, #111). The API reports `TN_VERSION`
(#103). No workflow deploys. The old deploy workflows were deleted because they could not work.
Staging (`tn-staging`, Ubuntu 24.04, no vCenter, no AD) runs `v1.2.0-rc3` with Moodle,
the ARC² Studio (auto-accepting test host) and the shipped content on; its v1.2.0
verification record is in `docs/release-notes/v1.2.0.md` (v1.0.0's in
`docs/release-notes/v1.0.0.md`). **Not done:** a run against real vCenter; Helm is
exercised only by the CI kind smoke install.

## Tests

The gate (`scripts/dod.sh`, the same selection as CI test-python) and the CI lanes are
listed in `RESUME.md`. Measured 2026-10-11 on macOS (`DOD_WEB=1 bash scripts/dod.sh`,
branch `claude/release-ready-gaps`), without PostgreSQL or OpenSearch: **5362 passed,
90 skipped, 0 failed**; Karma 631 of 631. The skips are mostly the PostgreSQL and
OpenSearch tests (CI provides both and fails if a PostgreSQL test skips), plus the
pfSense `php` checks (php CLI absent). Integration, e2e, Moodle, vcsim and Greyspace evidence comes
from CI runs, not from this count.

## Open

0. ADR 0014 follow-ups (2026-10-27): #273 (the installer does not pass
   `PLATFORM_TENANT_ID`, so a second tenant leaves nobody platform administrator), #289/#315
   (unit readiness), #310 (AD sync `ad_object_guid`), #313 (no Student console for class
   ranges). The code-vs-ADR differences found while writing the role guides are in the PR
   that added them.
1. v1.2.0 known limitations: `docs/release-notes/v1.2.0.md`. In short (checked 2026-10-12):
   all injectors simulated except the Greyspace breadcrumb; no real host sensor shipping;
   no team exercises or leaderboards (planned, R2); no i18n (English only; French is a
   planned milestone after R1); Alertmanager
   delivery unproven; the nightly backup does not stop the ARC² runner; abandoning a range
   leaves its hypervisor objects; vSphere simulator-proven only; the security gaps in the
   row above.
2. Live vSphere lab evidence pending (credentials, self-hosted runner): everything vSphere
   in v1.2.0 is simulator-proven only.
3. Content authoring remains the binding constraint (about 2,600 build hours).
