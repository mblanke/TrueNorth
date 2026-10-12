# Runbook — post-install acceptance (staging)

Script: `scripts/staging_acceptance.py`. Harness: `scripts/_inprocess_api.py` (shared with
`load_content_inprocess.py` and `arc2_batch_inprocess.py`). Tests:
`tests/scripts/test_staging_acceptance.py` (report and runner logic) and the CI `integration`
job, which runs the whole suite against the integration stack (`scripts/itest.sh acceptance`).

The suite proves each module's key flow end to end **on an installed host**: one check group
per module (17), each through the API's own routes, permission checks, CSRF and validation,
with the installed stack's real services behind them (PostgreSQL, Redis, OpenSearch, MinIO,
the LRS, the AI orchestrator, the Celery workers, the registered Moodle). Nothing is mocked.

## Run it

On the host, from the compose directory, as the bootstrap administrator:

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -p docker -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \
  --entrypoint python api /srcapp/scripts/staging_acceptance.py --admin <upn>
```

It runs in a one-off api container (the installed image, the host's checkout mounted
read-only at `/srcapp`), in-process: the token check is replaced by an existing user's
identity, as the batch drivers do. No password or token is used. It takes about a minute;
rate limits are waited out (429 `retry_after`), so a busy host can take longer.

Options:

| Flag | Use |
|---|---|
| `--admin <upn>` | an existing, active administrator (required) |
| `--modules a,b` | a subset, e.g. `--modules lms,detection` (fixtures are made on demand, so any subset works) |
| `--keep` | leave the test data in place (see [Test data](#test-data)) |
| `--json <path>` | write the report as JSON; mount a directory the api user (uid 10001) can write |
| `--absent a,b` | facilities this host does not have; their checks SKIP and say so (`--help` lists them) |
| `--strict` | any SKIP not listed for `--skip-profile` in `docs/release-notes/v1.2.0-permitted-skips.json` is a FAIL, declared or not; so is a check missing from (or not recorded in) that file's inventory. Exit 2 if the file or profile is unusable |
| `--skip-profile <name>` | with `--strict`: `staging` (default; only the two hardware skips) or `ci-integration` (what `scripts/itest.sh` uses) |
| `--permitted-skips <path>` | with `--strict`: another permitted-skips file |
| `--edge-url` | the edge as the container reaches it (default `https://nginx`) |
| `--host-config <dir>` | the host's config directory, if mounted (for `backup.env`) |
| `--lms-course <code>` | the catalogue course for the LMS checks (default `C103`) |
| `--moodle-course <code>` | the catalogue course for the Moodle loop (default: `--lms-course`); it must be published to the tenant's Moodle from an accepted release |
| `--vsphere-url`, `--vsphere-user`, `--vsphere-insecure` | when no vSphere connection is registered: a temporary one (password from `ACCEPTANCE_VSPHERE_PASSWORD`), removed afterwards |
| `--list` | list the checks and exit |
| `--verbose` | keep the API's own request logs (off by default: the table is the output) |

With the report kept and the optional checks enabled (backup environment, demo accounts
against Keycloak):

```bash
sudo install -d -o 10001 -m 0700 /srv/truenorth/acceptance
docker compose -p docker -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
  run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v /srv/truenorth/app:/srcapp:ro \
  -v /srv/truenorth/acceptance:/out -v /srv/truenorth/config:/hostcfg:ro \
  -e KEYCLOAK_ADMIN_USER=admin -e KEYCLOAK_ADMIN_PASSWORD="$(sudo cat /srv/truenorth/config/secrets/keycloak_admin_password)" \
  --entrypoint python api /srcapp/scripts/staging_acceptance.py --admin <upn> \
  --host-config /hostcfg --json /out/acceptance-$(date -u +%Y%m%dT%H%M%SZ).json
```

`backup.env` is checked for presence and for the variables it must set; values are never
printed. The Keycloak credential is the one the installer uses for `app.link_demo_accounts`;
the API service itself is not given it.

## Output

A table, `module | check | status | detail`, then `N passed, N failed, N skipped`. The JSON
(`--json`) has the same rows plus each check's duration and whether a SKIP was declared, and
`meta`: run tag, administrator, host, version, `PROVISIONER_BACKEND`, which account played each
role, what cleanup left behind.

Exit codes: `0` nothing failed; `1` at least one FAIL; `2` the suite could not start (the
administrator was not found, a bad argument).

A SKIP always states its reason. Two kinds:

- **declared**: a facility named in `--absent`, or a check this suite cannot make on any
  staging host (below). Being declared is not enough under `--strict`: only the skips the
  profile lists in `docs/release-notes/v1.2.0-permitted-skips.json` stand (module, check and
  reason), and the release matrix (`docs/release-notes/v1.2.0-module-matrix.md`) says why.
- **found**: something the run discovered missing (no Moodle platform registered, no accepted
  cmi5 release on a published course, no demo accounts). Read these: on a host that should have
  the thing, they are findings.

## What each module checks

Roles: the installer's demo accounts (`tn_load_shipped_demo`) when they exist with the
expected role — `instructor.demo`, `trainee1.demo` (the participant Student),
`trainee2.demo` (a Student on nothing here), `observer.demo` — else users this suite creates
(`acceptance-<role>@truenorth.test`, deactivated afterwards).

| Module | Checks |
|---|---|
| infra | `/health/live`, `/health/ready`; `/health/deep` with all seven components plus `audit_export` (the signed daily audit export is current; ADR 0011) (OpenSearch yellow passes: a single node cannot place replicas; anything else not healthy fails); `/metrics` 401 without `METRICS_SCRAPE_TOKEN`, Prometheus text with it; through nginx: `/health` and `/api/health` 200, `/api/metrics` 404; `backup.env` present and sets `BACKUP_DIR` |
| platform-core | a user for each role; Student and Observer refused staff routes (403: audit log, users, booking, ranges, exercises), instructor refused AI config; tenant scoping (every role in the admin's tenant, rows carry it, foreign ids 404); the audit log records the exercise's create and start; demo accounts' subjects match Keycloak (needs the admin credential) |
| web-shell | the Angular shell served through nginx for `/`, `/dashboard`, `/learning/courses`; for 17 main routes, the API endpoint each page loads answers each role as `app/rbac.py` says (50 role checks) |
| content | all shipped course files (`content/courses/*.yaml`, 44) have their catalogue course with modules; every shipped template, scenario and Sigma rule is loaded; the VM image catalogue is not empty |
| curriculum | QSP qualifications with performance objectives, learning paths, developmental progression, the curriculum map; a Student's own map |
| lms | the participant Student enrolled on C103 sits its quizzed module's quiz (answers hidden from them), passes, and the module progress and the PO on their curriculum map show completed; cmi5: on an accepted release of a published course, the Student launches an AU, fetches the token once (a second fetch is refused), reads `LMS.LaunchData` and learner preferences, sends `initialized` and `terminated` through TrueNorth's LRS endpoint, and the statement is read back from the LRS; each registered Moodle platform is reachable and its results list answers; **Moodle loop (integration, bypasses browser)**: see below |
| scenario-engine | the shipped `incident-response-drill` validates and its exercise (the installer's demo exercise, or a pending `acceptance-plan-…`) carries exactly the scenario's objectives and timeline; `acceptance-exercise-<tag>` starts, **worker-scenario** fires its inject (recorded `fired`), it stays running after the timeline (real backend), pauses (a second pause 409) and resumes |
| telemetry | three events indexed into a range, MITRE-tagged, found by administrator and Observer through the search API; a query outside the grammar 422; Students get 404 searching a range they are not on and 403 writing telemetry |
| detection | a draft booking ties the exercise to C103 (the roster), timed over the exercise's run (a booking seats its course only while it overlaps the exercise); a Student not enrolled there is refused (403); the participant's detection query is credited (`achieved`, score 40/50, objective achieved, `on_target` hidden from them); a repeat is refused 409 |
| reporting-aar | complete the exercise (twice: 409), generate the AAR (HTML names the exercise, objectives and timeline), the PDF is a real PDF, a Student may read the AAR, a missing one is 404 |
| ai-arc2 | `/ai/detection-draft` returns text from the configured AI orchestrator; `/arc2/status` enabled and runs listed |
| ranges-labs | template validation accepts a good template and refuses a broken one; a range is created (recording the backend), a document stored in and read back from MinIO, the range deleted; a Student not enrolled on a course is refused a lab (403); staff list lab sessions |
| provisioners | the registered vSphere connection (or a temporary one, `--vsphere-url`) passes its connection test and discovery finds hosts; an instructor may not test it; the inventory summary |
| scheduling | capacity and a one-VM fit check; a booking 45 days out is created, listed, readable by an Observer, refused to a Student, and cancelled |
| noise-engine | presets; on a range from a noise-enabled template: not configured at first, a dry-run deploy plans the agent and registers nothing, the profile enables at level 100 and stops at 0; a template without a `noise:` block 409; a Student 403 |
| greyspace | corpus `t0` available; a range from the shipped Greyspace template reads its block, attaches it (`configured`, no problems), the config plans DNS, resolver and web farm, an unknown site pack 422, detach |
| wiki-helpdesk | a wiki space and published page (a Student reads it); a ticket assigned to the instructor reaches the instructor's inbox, is marked read, is not in the actor's inbox and is 404 to anyone else |

### lms / Moodle loop (integration, bypasses browser)

M1's Student loop on the installed host, without a browser (`scripts/_moodle_student.py`):

1. The suite's own Student, `acceptance-moodle-student@truenorth.test` (never a demo account,
   so the participant's record is not touched), is enrolled on the course (`--moodle-course`,
   default `--lms-course`).
2. TrueNorth mints the sign-in ticket in-process (`POST /integrations/moodle/sso` as that
   Student, the route the SPA's "Open in Moodle" calls).
3. The suite POSTs it to `sso.php` and, in that Moodle session, opens the course, a page, and
   the first quiz: it starts an attempt, answers the first page, leaves, comes back ("Continue
   your attempt" must reopen the same attempt), answers the rest from TrueNorth's answer key
   for the course (`GET /quizzes/{id}/questions`) and submits. These are Moodle's own pages
   and forms, as plain HTTP; no JavaScript runs.
4. After Moodle's 5 s settle window it triggers a pull (`POST …/moodle-results/pull`; a pull
   already running is waited out), then reads `GET /integrations/moodle/results` as the
   Student: the grade of **this** attempt (Moodle's attempt id; an older plugin reports none
   and is matched on Moodle's time instead, and says so), a completed activity, the
   enrolment, and the sync `current`. With every answer keyed, the grade must be full marks.
5. The participant Student's progress on the course must not go down.

How it reaches Moodle: like TrueNorth's own server-side calls, to the platform's `base_url`
(e.g. `http://moodle-default:8080` on `tn-backend`) with the public Host header
(`app.lti13.platform_route`). It needs no token, password or host-side helper: the session is
the one TrueNorth's ticket opens, and the installer's registration (`install_cli register`)
already records both addresses.

Needs: a registered Moodle (the installer's `moodle-<node>`), and the course published to it
from an accepted release (`docs/runbooks/moodle.md` section 2). C103 has no release until the
ARC² batch publishes one; until then run with `--moodle-course` naming a course that has, or
the check SKIPs ("not published to …"), which `--strict` on the `staging` profile fails: the
loop is mandatory on staging (`docs/release-notes/v1.2.0-module-matrix.md`). The CI
`integration` stack has no Moodle: its `ci-integration` profile permits the skip, and the CI
`moodle` lane runs the same loop in Chromium (`tests/integration/test_moodle_student_journey.py`).

## What it cannot verify, and why

- **Building anything on the hypervisor.** Staging's provisioner is `vsphere_api` against a
  read-only simulated vCenter. The suite never provisions: ranges stay `created`, the exercise's
  one inject (`simulated_execution`) writes synthetic records and touches no host, and the
  booking is far enough out (45 days, cancelled at once) that the scheduler clock never builds
  it. So **worker-provision** is not exercised; worker-scenario and worker-telemetry are.
- **Lab builds** (declared SKIP): cloning lab VMs and waiting for VMware Tools needs a
  writable, real vCenter (`tests/integration/test_lab_sessions_vsphere.py`).
- **Noise agents on VMs** (declared SKIP): a real deploy installs agents on built range VMs.
- **The browser.** The SPA is checked as served HTML plus the API each page loads, per role;
  rendering and sign-in through Keycloak are the Playwright lane's (`e2e`). The Moodle loop
  drives Moodle's pages without a browser; the loop in Chromium is the CI `moodle` lane's, and
  on staging the click-through in `docs/runbooks/moodle.md` ("Student loop on staging").
- **MFA sign-in.** The suite acts in-process (`tn_session=inprocess`, exempt and audited), so
  it does not sign in through Keycloak. Run the **MFA drill** by hand on staging
  (docs/runbooks/break-glass.md "Drill"): an instructor enrols an authenticator at first
  sign-in (and is asked for one more code: the step-up), signs in again with a code, the
  break-glass playbook resets it, and the next sign-in shows enrolment again; check the
  `mfa_reset` audit row. A Student and an LTI-launched Student sign in without a code. The
  Keycloak half is proven on every change by `scripts/check_keycloak_mfa.sh` (CI).
- **The edge certificate.** nginx is reached from inside the stack without verifying TLS (the
  installer's certificate is often from an internal CA this container does not carry).

## Test data

Everything is named `acceptance-…`. Removed afterwards (unless `--keep`): the temporary range,
noise and Greyspace ranges, the booking (cancelled), the roster booking, the wiki space
(archived), the ticket, a temporary vSphere connection, users this suite created (deactivated).
Kept and reused by name on the next run, because exercises keep them as history:
`acceptance-template`, `acceptance-range`, `acceptance-telemetry-range`,
`acceptance-scenario`, `acceptance-noise-template`, `acceptance-plan-<scenario>` (when there
is no demo exercise). Each run adds one completed `acceptance-exercise-<tag>` (exercises
cannot be deleted) and leaves the participant enrolled on C103 (and on the cmi5 course), with
the quiz attempts and statements it made. The Moodle loop's Student
(`acceptance-moodle-student`) is deactivated afterwards and reactivated by the next run, so it
keeps one TrueNorth id and one Moodle account (`tn-<id>`); each run adds one finished quiz
attempt in Moodle, which the next pull records as that Student's grade (the highest, as
Moodle grades), and leaves them enrolled on the course. Anything cleanup could not remove is printed
(`cleanup: …`) and listed in the JSON.

## When a check fails

- **detection: `0 events matched`.** Detection credit is windowed on `truenorth.ingested_at`,
  stamped by OpenSearch's ingest pipeline. Re-run the bootstrap the installer runs
  (`python -m telemetry.pipelines.bootstrap`, role `tn_telemetry`).
- **provisioners: `Connection failed: … /api/session` 404.** The API's vSphere connection
  backend (`control-plane/api/app/hypervisor_backends/vsphere.py`) speaks only the vCenter
  Automation API (`/api/session`) on port 443: it ignores the connection's port, and has no
  fallback to the legacy `/rest` session that older vCenters and vcsim offer (the worker's
  provisioner has one). A simulator on another port, or one without `/api`, fails here. That is
  a defect in that backend, not in the suite.
- **lms cmi5: SKIP `no accepted course release … on a published course`.** No course with an
  accepted ARC² release is published yet (`scripts/arc2_batch_inprocess.py` publishes them).
- **lms Moodle loop: SKIP `… is not published to …`.** Publish an accepted release of the
  course to the tenant's Moodle, or name one that is with `--moodle-course`.
- **lms Moodle loop: `sso.php => 404: a different moodle account already uses this email
  address`.** A Moodle account that TrueNorth's sign-in did not make holds
  `acceptance-moodle-student@truenorth.test` (Moodle never takes an account over by email).
  Remove that Moodle account as the break-glass administrator, then re-run.
- **lms Moodle loop: `sync after the pull: … failing`.** Read `last_error` on
  `GET /api/integrations/platforms/{id}/moodle-results` (`docs/runbooks/moodle.md` section 4).
- **a FAIL naming a route and a status** in web-shell: the role's access changed from
  `app/rbac.py`; either the route or the table in `ROUTE_BACKING` is wrong. Find out which
  before changing either.

## In CI and locally

`scripts/itest.sh acceptance` runs it against the integration stack (part of `make itest` and
the CI `integration` job): OpenSearch bootstrapped, the shipped content and demo people loaded
in-process (`load_content_inprocess.py --publish --demo`), then the suite with
`AUTH_DISABLED=false` (each role its own user), `PROVISIONER_BACKEND=vsphere_api` (ranges
recorded as staging's are), `--strict`, and `--absent` naming what that stack does not run
(`ACCEPTANCE_ABSENT`: edge, keycloak, minio, lrs, moodle, backup, vsphere, metrics-token,
arc2). The report is uploaded with the job's artifacts (`acceptance.json`).
