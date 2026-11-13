# TrueNorth code review improvement plan

Date: 2026-10-06
Revision: 3. Scope and priorities refreshed to current `main`.
Status: proposed delivery plan. No finding below is fixed on `main`, and no experiment below has been run on it.
Baseline: `main` at `0fa60ee`.

**What changed from revision 2**

- Rev 2 targeted `main@083aeb7` plus the feature worktrees. Since then `main` gained immutable course releases, Moodle publication and student lab sessions, and its authoring path no longer uses the API-runner Course Studio. Rev 3 scores what ships on `main`.
- The API-runner Studio is retired. Its findings (CR1-01 and CR1-02) and PRs are kept as history only.
- The scoring rules no longer allow a zero, and they make identity validation and operational recovery mandatory where those features ship.

**History**

- Rev 2 lives in the repository root as `codereview1.md`, kept by the owner.
- The off-`main` hardening work (PRs #8–#43, branch `hardening/integration-candidate`, merge-base `083aeb7`) holds the fixes and evidence for rev 2's findings. None of it is on `main`; section 6 says which parts are re-landed.

## 1. Scope

| Ships and is scored | Source on `main` |
|---|---|
| Offline ARC² authoring: request → staged bundle → release tarball (learner/platform/instructor parts) | `.claude/commands/arc2.md`, `.claude/agents/arc2-*.md`, `tools/arc2/{check,qa,cmi5,lab_profile,release}.py` |
| Course releases: upload, accept, supersede, enrollment pinning, instructor-material separation | `control-plane/api/app/course_releases/`, `routers/course_releases.py`, migration `a9c0d1e2f3a4` |
| Moodle course publication: stage → verify → activate, retry, resume; signed sync tickets | `course_publishing/`, `moodle_backends/`, `routers/course_publications.py`, migration `b0d1e2f3a4b5`, `docs/adr/0004-moodle-course-publication.md` |
| Student labs: launch, quotas and queue, reset, expiry, teardown, lab tokens, LTI lab launch | `lab_sessions/`, `console_backends/`, `routers/lab_sessions.py`, `routers/integrations.py` (`_launch_lab`), `worker/lab_tasks.py`, migration `c1e2f3a4b5c6` |
| Range lifecycle and power, which labs build on | `routers/ranges.py`, `worker/tasks.py`, provisioners |
| Tenancy and RBAC on all of the above, and the `/ws` event stream | `rbac.py`, `tenancy.py`, `main.py` |
| Course Studio web: upload, review, accept, publish, labs | `features/course-studio/`, `core/services/course-studio-api.service.ts` |

**Retired:** the API-runner Course Studio (`routers/arc2_studio.py`, `tools/arc2/{runner,confine,egress}.py`). It is not on `main`, and its findings are history.

**Separate features, outside this scope until they ship:**

- Wiki and tickets (#14; not on `main`).
- Background noise (#29; CR1-04).
- Windows roles (`wip/windows-roles-snapshot-2026-10-05`).
- The Moodle per-tenant course farm.
- Greyspace.
- The scheduler (#41–#50).
- vSphere features beyond what labs need.

A feature enters scope only when it is on `main`. It then needs its own feature row in section 2.

## 2. Scoring

Score five checks per area, each 0–2:

- **0:** absent, failing or unverified.
- **1:** implemented, with partial evidence.
- **2:** demonstrated by the acceptance evidence in section 4 on the candidate revision.

Record the evidence link and reviewer for each check, using the review-record fields and evidence policy of rev 2, which are unchanged.

**Rules (new in rev 3):**

1. **No zeros.** Every applicable check scores at least 1; a 0 anywhere blocks its row. Checks marked `*` must score 2. A row passes at 8/10 or more with all its `*` checks at 2. Rows cannot compensate for each other.
2. **Applicable** means the check's code path is reachable in the release configuration. A check is N/A only when a flag or route disables that path in the shipped configuration, and the integrator signs it off. "Not tested" is never N/A.
3. **Per-feature gating.** Each shipped feature has a feature row: releases, publication, labs, ranges, LTI/identity. A row lists its experiments from section 4 plus a tenant/role denial matrix. A feature with a failing row caps every area it touches at 7.

| Area | Five checks (`*` = must score 2) |
|---|---|
| Architecture | Adapter boundaries; generated contract consistency\*; one owner for operation/allocation truth\*; focused service responsibilities; migration/backward-compatibility design. |
| Readability | Traceable critical control flow\*; accurate comments/invariants\*; explicit error outcomes; clear typed interfaces; focused diffs and maintenance documentation. |
| Testing | Known-defect regressions fail before / pass after\*; deterministic failure and concurrency coverage on PostgreSQL\*; production-equivalent database/broker evidence; real browser journeys; reproducible required gates\*. |
| Security | Tenant/role denial matrix\*; **release-artifact and instructor-material isolation\***; **identity, LTI and signed-token validation\*** (LTI login and lab launch, lab tokens, Moodle sync tickets ship); network/credential boundaries\*; dependency and container verification. |
| Operational reliability | Durable accepted work\*; ordered/idempotent operations\*; bounded timeout/restart recovery\*; collision-free resource lifecycle\*; **observable failures and recovery runbook\***. Background processing ships (publication runner, lab sweeper, Celery), so this check requires a recovery drill from F12, F17, F18 or F19. |
| Integration readiness | Frozen source/dependency manifest\*; fresh-install and populated-upgrade migrations\*; complete selected journeys\*; applicable simulator/lab evidence\*; tested release/rollback compatibility. |

## 3. Known-defect register

A finding marked "to reproduce" is a candidate traced from source. It is not a confirmed defect until its failing test exists. The first step of every fix is that failing test, shown failing on `main`.

| ID | Pri | Finding (source on `main@0fa60ee`) | Status |
|---|---|---|---|
| CR1-05 | P1 | `POST /ranges/{id}/stop` sets `state = stopped` and sends nothing to the worker. There is no `/start` (`routers/ranges.py:531–545`). | Carried; reproduced on the old base |
| CR1-06 | P1 | Range tasks have no operation record, fencing or lease. With `task_acks_late` and a 3600 s visibility timeout, a redelivered or duplicated task can build or tear down a second time while the first runs. | Carried; reproduced on the old base (two provisioner calls) |
| CR1-07 | P1 | `/ws/{channel}` accepts any connection on any channel, with no authentication or tenant scope (`main.py:369–372`). | Carried |
| CR1-10 | P1 | Lab tasks are queued in memory (`db.info`) and sent after commit. A process that dies between commit and `flush_outbox` loses the task, and `pending` covers only broker refusals (`lab_sessions/service.py:185–200`). A provisioning lab then waits for its timeout. | To reproduce |
| CR1-11 | P1 | Labs provision, reset and tear down through the same unfenced range tasks (CR1-06). An `end` or `reset` can dispatch a destroy or restore while a provision is still running. | To reproduce |
| CR1-12 | P1 | Quotas count running labs per user and tenant without a tenant-wide lock. Launch locks only the student's own row (`service.py:384`; `_quota_problem` from `:307`). Concurrent launches by different users can exceed tenant session or vCPU limits on PostgreSQL. | To reproduce |
| CR1-13 | P1 | Release, publication and lab tests run on SQLite only. `with_for_update`, `skip_locked` and the one-accepted-release partial index are unproven on PostgreSQL. | To reproduce |
| CR1-14 | P2 | Release immutability is enforced only by ORM `before_update` and `before_delete` hooks (`course_releases/models.py:126–150`). Core or raw SQL updates bypass them. | To reproduce |
| CR1-15 | P2 | Publication `claim` is atomic, but `_step` renews `lease_until` with no holder check (`course_publishing/service.py:98–110, 268–273`). A step that outlives the 15-minute lease lets a second process (startup resume or retry) act on the same publication. | To reproduce |
| CR1-16 | P2 | The lab sweeper starts in every API process (`main.py:69–74`). Sessions are serialised by a per-session lease; overlap when one advance outlives its claim is unproven. | To reproduce |
| CR1-17 | P2 | Lab tokens are checked for signature, audience, type and session (`lab_sessions/tokens.py:46–55`), and `_token_session` adds only the user match (`routers/lab_sessions.py:109–119`). Neither checks session state or enrollment. Whether each `/lab-access` operation refuses an ended or withdrawn session further down is unverified. | To reproduce |
| CR1-18 | P2 | The LTI lab launch enrols the student in the course named in the link (`routers/integrations.py:370–399`). It is unverified that the launch's LTI context or platform maps to that course, or that the course is published to that platform. | To reproduce |
| CR1-19 | P3 | Lab tokens travel in the URL fragment. Whether the web app removes the fragment from history and from any client-side logging is unverified. | To verify |
| CR1-20 | P3 | The platform JWKS is fetched on every launch, with no cache or key-rotation handling (`lti13.py:184–187`). | Carried (fix in #13) |
| CR1-21 | P3 | Two outbox designs: the lab in-memory outbox, and the range-operation row outbox to be re-landed. | Design item for R1 |
| CR1-04 | P1 | Noise management addresses collide across ranges. | Only if noise ships |

Retired as history (API-runner Studio): CR1-01 (tenant ownership) and CR1-02 (runner deadline). CR1-03 (wiki concurrency) moves with the wiki feature.

## 4. Acceptance experiments

Rev 2's experiment rules still apply:

- Controlled interleavings use barriers, never sleeps.
- Each experiment runs through the real route or task path.
- PostgreSQL is required for any locking claim and a real Redis for any delivery claim.
- A real Moodle or vSphere is required where a fake cannot establish the behaviour.

F05–F07 (range power, stale-task fencing, recovery) are carried unchanged.

| Test | Controlled trigger | Pass condition | Environment / extends |
|---|---|---|---|
| F10 release pinning | Enrol and launch on v1; accept v2; relaunch. Two concurrent accepts (v2 and v3). A Core or raw SQL update of `release_digest`. | The enrollment keeps v1. Exactly one accepted release, and the version never goes backwards. The raw update is refused by the database, or the gap is recorded as accepted risk. | PostgreSQL; `tests/api/test_course_releases.py` |
| F11 instructor-material separation | Bundle with instructor files in the learner part. Student calls to both bundle routes, `/lab-sessions`, `/lab-access`. Inspect the Moodle payload. | Upload refused. No instructor or platform bytes on any student surface or in the Moodle payload. Students get 403/404 on both bundles. | API; `test_course_releases.py`, `test_course_publications.py` |
| F12 interrupted publication | Kill the process after each step (staging, verifying, activating, stage delete), then restart. Two API processes resume at once. Moodle unreachable. Stage delete fails. One step outlives the lease. | One live course; students never see a partial course. One resumer acts at a time. A failed publication is retryable. A stage leftover is only a warning. No concurrent upserts. | PostgreSQL + fake Moodle, then a real Moodle (`tests/integration/test_moodle_publish.py`) |
| F13 sync-ticket validation | Replay a `jti`; wrong `tid`; changed body; ticket older than 60 s; another tenant's key. | Each refused by the Moodle plugin, with no course change. | Real Moodle plugin (`local_truenorth`) |
| F14 lab-token isolation | Session A's token on session B; another user's token; expired token; token after end, reset or enrollment withdrawal; token signed by another key; another `typ`. | 401/404 every time, with no state change. | API; `tests/api/test_lab_sessions.py` |
| F15 quotas | N concurrent launches by different users in one tenant. Per-tenant vCPU and network limits. A queue of waiting labs. | Limits never exceeded; first-in-first-out order kept; no deadlock. | PostgreSQL, two or more sessions |
| F16 reset | Snapshot restore vs rebuild. Reset during provisioning, baselining or another reset. Worker crash mid-restore. | Correct refusal or completion. Network lease kept on rebuild. Lifetime not extended. Recovers or fails visibly. | PostgreSQL + real broker; `tests/worker/test_lab_ranges.py` |
| F17 expiry | Idle and maximum-lifetime expiry, with the sweeper stopped and restarted, and with two API replicas. | Each session expires once; teardown dispatched once. | PostgreSQL, two processes |
| F18 resource cleanup | End or expire during provisioning. Worker killed after destroy. `reconcile_lab_vms` after a crash. Retired ranges. | No VM, port group or range left. `lab_network_leases` freed only after reconcile proves the resources are gone. A teardown that keeps failing becomes visible to an operator. | Real broker, then vSphere (`tests/integration/test_lab_sessions_vsphere.py`) |
| F19 task loss | Crash after commit and before send, for labs and for range operations. | The task is delivered after restart, without waiting for a timeout. | PostgreSQL + real broker |
| F20 LTI lab launch | Bad state or nonce; replayed launch; wrong deployment; another tenant's platform; a link to a course not published to that platform. | 401/403. No enrollment, session or token created. | API; `tests/api/test_lti13.py` |

## 5. Delivery order

Fresh PRs off `main@0fa60ee`, each self-contained with its failing-first tests and contracts. The old stacked PRs close once their content is re-landed or retired; this document does not close them.

| Unit | Content | Re-lands from | Closes once replaced |
|---|---|---|---|
| R0 | Baseline on `0fa60ee`: toolchain, PostgreSQL test fixture that R1–R3 use for CR1-13, tests never reach a real broker, this document | #8, #43, part of #22 | #8, #24, #27, #40 |
| R1 | Range-operation substrate, with labs routed through it (CR1-05, -06, -07, -10, -11, -21; F05–F07, F19). Contents below. | #17, #18, #30, #31, #39, `/ws` part of #32 | #17–#19, #30–#33, #39 |
| R2 | Releases and publications: database-level immutability, a publication lease with a holder (CR1-14, -15; F10–F13) | new | — |
| R3 | Lab lifecycle: tenant-wide quota lock, sweeper timing, token checks against session state (CR1-12, -16, -17; F14–F18) | new | — |
| R4 | Security: LTI context-to-course mapping, JWKS cache and rotation, non-root images, ops-center tenancy, sealed secrets (CR1-18, -19, -20; F20) | #13, #34, #36 | #13, #34, #36 |
| R5 | Evidence: PostgreSQL concurrency, real broker, real Moodle and vSphere runs; populated upgrade on `main`'s chain (head `c1e2f3a4b5c6`) | #22, #23, #35; #20 and #33 where labs need shared allocation | #20–#23, #33, #35 |
| R6 | Independent re-score under section 2 | — | — |

**R1 contents:**

- Durable operation records with an outbox shared with labs.
- Fencing and per-range leases.
- One sender per operation.
- Soft and hard task time limits, with cancellation that lets cleanup run.
- The lease kept while a cut-off hypervisor call may still run.
- Real `/stop` and `/start`.
- An authenticated, tenant-scoped `/ws`.

**Other dispositions:**

- **Re-land separately when wanted, with new ADR numbers** (0004 is the Moodle publication ADR on `main`): #12 (formatting), #21 (scoring/xAPI, worker SQL), #28 (scenario objectives), #37 (in-app push), #38 (exercises refresh).
- **Close as retired:** #9, #10, #11, #15, #16, #25, #26 (API-runner Studio).
- **Out of scope:** #14 (wiki), #29 (noise; CR1-04 returns with it), #41–#50 (scheduler), #3 and #6 beyond what labs need.

**Start with R0, then F19's failing test, then R1.** Labs already build ranges at up to 20 VMs, so R1 has the largest blast radius. R2–R4 are independent of each other and can proceed in parallel.

**Lessons carried from the off-`main` reviews.** Five independent review rounds of the old R1 work found defects introduced by the fixes themselves. R1 must test each of these on PostgreSQL and on a real worker:

- Overlapping deliveries.
- A hard limit that skips cleanup.
- Abandoned coroutines.
- Leases released while a hypervisor thread still runs.
- Unfenced snapshot tasks.

## 6. Migrations, rollback and verification

- **Migrations:** rev 2's migration contract is unchanged. `main` has a single Alembic head, `c1e2f3a4b5c6`. Re-landed migrations are re-parented onto it and never merged as a second head.
- **Gates:** `bash scripts/dod.sh` runs for backend changes, and `DOD_WEB=1 bash scripts/dod.sh` for UI changes and the candidate.
- **Concurrency:** concurrency claims also need the PostgreSQL-gated tests (`TEST_POSTGRES_ADMIN_URL`), and delivery claims a throwaway Redis (`TEST_REDIS_URL`). The suite must never reach the developer's real broker.
- **Lab, Moodle and vSphere evidence** uses the gated integration tests and runs against the owner's lab with the owner's credentials. Missing credentials leave the check BLOCKED, never passed.

## 7. Validation of this document

This document changes no runtime behaviour. On 2026-10-06 these citations were read directly on `main@0fa60ee`:

- `routers/ranges.py:531–545` (stop sets the state);
- `main.py:369–372` (unauthenticated `/ws`) and `:63–74` (publication resume and lab sweeper at startup);
- `lab_sessions/service.py:185–200` (in-memory outbox), `:307` (quota counts) and `:384` (user-row lock);
- `lab_sessions/tokens.py:30–55` and `routers/lab_sessions.py:109–119` (token checks, no state check);
- `course_releases/models.py:126–150` (ORM-only immutability);
- `course_publishing/service.py:98–110, 268–273` (claim vs step renewal);
- `routers/integrations.py:370–399` (lab launch);
- `lti13.py:184–187` (JWKS fetch).

Every finding marked "to reproduce" or "to verify" is a traced candidate, not a confirmed defect. No experiment in section 4 has been run on `main`.
