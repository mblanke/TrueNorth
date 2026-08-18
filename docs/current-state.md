# TrueNorth — Current State

**Reconstructed 2026-08-18 from the working tree, not from documentation.**

Method per `/truenorth-baseline`: trust order is code > configuration > running services >
uncommitted files > tests > docs. Every classification below cites evidence. Where the
evidence was thin the label is `UNTESTED`, not a guess.

Labels: `IMPLEMENTED` `PARTIAL` `BROKEN` `UNTESTED` `UNDOCUMENTED` `PLANNED`
`STALE-DOC` `DUPLICATED` `DEPRECATED`

> **Scope note.** This host is the AI engineering / development box. It does **not**
> provision infrastructure — `PROVISIONER_BACKEND=mock` is the intended setting here, and
> real provisioning runs on a separate demo environment on another network. Mock is not a
> defect and is not reported as one.

---

## Summary

The platform is substantially built and the curriculum spine is real. The gap between
the codebase and `docs/` is wide enough that the documentation should be treated as a
statement of intent. The binding constraint on the product is **content authoring**
(~2,600 build hours, ~57% gated on cleared humans and purchased courseware), not code.

Two structural defects found that documentation would never surface: a **duplicated
alembic tree** where the documented `make migrate` targets the wrong one, and a
**duplicated injector tree** that is dead code.

---

## Control plane API — `IMPLEMENTED`

**292 routes across 28 routers**, counted from the running container
(`app.main:app`, not from docs). Largest surfaces: `/proxmox` 35, `/directory` 18,
`/ai-config` 17, `/ranges` 16, `/exercises` 16, `/qsp` 12.

Test coverage is **narrow and concentrated** — `PARTIAL`. Endpoint paths actually
exercised by the suite: `qsp` (22 calls), `ranges` (17), `tenants` (9), `items` (7),
`exercise-forge` (7), `health` (4), `audit-log` (4). Large surfaces including
`/proxmox`, `/directory`, `/ai-config`, `/scheduling`, `/quizzes`, `/threat-intel` and
`/hypervisors` have **no direct endpoint tests**.

## Database migrations — `DUPLICATED` / actively hazardous

Two alembic trees exist:

| Tree | Revisions | Status |
|---|---|---|
| `alembic/versions/` (repo root) | 2 | **Stale.** `alembic.ini` hardcodes `postgresql+psycopg://forge:forge@localhost:5432/forge` |
| `control-plane/api/alembic/versions/` | 12 | **Live** — this is what the running API container carries |

`make migrate` runs `alembic upgrade head` from the repo root, i.e. against the **stale
two-revision tree** and a database name (`forge`) that is not the deployed one. The
documented migration command targets the wrong tree. Fix before anyone runs it in anger.

## Scenario engine — `IMPLEMENTED`, with `DUPLICATED` dead code

44 Python files. Validators are real and match the content-pack contract:
`opensearch_query.py`, `deliverable_check.py`, `manual_ack.py` over a `base.py`.
Injectors cover AD attack, C2 beacon, DNS spike, phishing, file ops, HTTP burst,
identity changes.

**Two injector trees**: `scenario-engine/injectors/` (6 files) and
`scenario-engine/scenario_engine/injectors/` (11 files), with different contents. Tests
and application code import the packaged path (`scenario_engine.*`); the top-level
`scenario-engine/injectors/` has **no importers** — dead code that reads as live.

## QSP / CFITES curriculum spine — `IMPLEMENTED`

`Qualification → PerformanceObjective → EnablingObjective`, ingested from
`crosswalk.csv` (16 rows / 4 QSPs). Learner progress resolves through the LMS chain
`PerformanceObjective ← CourseModule.po_id → Course ← Enrollment → ModuleProgress`
(`qsp_progress.py`), batched to a fixed query count — asserted by tests, not assumed.

Framework mapping is now reconciled: 15/16 rows carry NICE work-role + task ids. The
16th is a documented `needs_spec` placeholder. **Version pin was wrong and is
corrected** — rows claimed NICE Components `v2.1.0` while carrying SP 800-181 rev-1
identifiers (`T0xxx`, `PR-CDA-001`); now `SP800-181r1`. Red Analyst rows carry
`DCWF-TODO` because only a NICE mapping was available.

The DP ladder deliberately leaves DP3-5 rank labels **blank** — no on-box QSP
establishes them, and a plausible guess would read as authoritative to CAF users.

## Content — `PARTIAL`, and the real bottleneck

`crosswalk.csv` status: 7 `todo`, 4 `offensive_author`, 3 `cots_gate`, 1 `needs_spec`,
1 `example`. Only **two** scenario directories exist on disk (`PO_009_EXAMPLE`,
`PO_007`), and `PO_007` is ~132 lines — the **spec layer only**: no staged pcaps, no
golden image built, no range ever provisioned.

Golden images: 20 Packer files generated, **zero built**. `win7-sp1` needs entitled
volume-license media (procurement); `win-xp-sp3` disabled pending EO justification.

## Worker / Celery — `IMPLEMENTED` (16 tasks) · `UNTESTED` end-to-end

Four worker containers running (provision, scenario, telemetry, plus flower). Unit
tests exist (`tests/worker/`); full lifecycle tests are **skipped by design** — they
require external services (27 skips across `tests/integration/`).

## Telemetry, storage, identity — `IMPLEMENTED`

OpenSearch 2.13 + Dashboards, MinIO, PostgreSQL 16 + pgbouncer, Redis 7, Keycloak 24,
an LRS (`yetanalytics/lrsql`), OTel collector, telemetry-pipeline — all running.
xAPI (`xapi.py`) and AAR/PDF reporting (`reporting.py`, fpdf2) present with tests
(`test_xapi.py`, `test_reporting.py`).

## Sensor integrations — `PARTIAL` / `PLANNED`

`suricata` 13 files, `zeek` 8, `velociraptor` 6, **`helk` 1**. The architecture docs
present HELK as a core component; the code does not support that. Treat HELK as
`STALE-DOC`/`PLANNED`.

## Multi-tenancy — `IMPLEMENTED`, `UNTESTED` at the boundary

`tenant_id` appears 58 times in `models.py`. There is no test asserting that a query
cannot cross tenants — the highest-value missing test in the repo, since a single
missing filter leaks silently and no existing test would notice.

## Security posture — `PARTIAL`

Fixed this session: the local LLM endpoint was reachable **unauthenticated on the LAN**
(`133.1.14.240:8080`) and now requires an API key (effective at next restart); a live
LiteLLM master key was removed from `.claude/settings.local.json`.

**Still open:** OpenSearch `:9200` answers unauthenticated on the LAN and will list
indices, including a `curriculum-*` index. The Angular dev server (`:4200`) and
ai-orchestrator (`:6000`) are also bound to `0.0.0.0`.

## CI/CD — `UNTESTED`

Five workflows exist (`ci.yml`, `deploy-dev`, `deploy-prod`, `packer-build`,
`terraform-plan`). No evidence in-tree that they currently run — `README.md` shows a
static "CI passing" badge and a "coverage 87%" badge with no coverage artifact to
support it (`STALE-DOC`).

## Test baseline

**456 passed, 27 skipped** (`.venv/bin/python -m pytest -q`). The 27 skips are
integration tests needing external services — skipped by design.

There is no `karma.conf.js`; frontend tests need `--karma-config`, a `CHROME_BIN`, and
a `--no-sandbox` launcher. `pyproject.toml`'s dependency list is stale against
`control-plane/api/requirements.txt`, and `telemetry-pipeline` conflicts with `api` on
`httpx` (0.27.0 vs 0.27.2), so a single combined install is impossible.

---

## Ranked recommendations

1. **Fix the alembic duplication** before someone runs `make migrate`.
2. **Delete `scenario-engine/injectors/`** (dead) or make it the single source.
3. **Add a tenant-isolation test.** Highest-value missing coverage.
4. **Close OpenSearch `:9200`** on the LAN.
5. **Correct the README badges** or wire up real CI reporting.
6. Treat HELK as planned, not delivered, in the architecture docs.
