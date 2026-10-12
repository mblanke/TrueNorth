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
| `--strict` | any SKIP not declared up front (`--absent`, or a check no staging host can make) is a FAIL |
| `--edge-url` | the edge as the container reaches it (default `https://nginx`) |
| `--host-config <dir>` | the host's config directory, if mounted (for `backup.env`) |
| `--lms-course <code>` | the catalogue course for the LMS checks (default `C103`) |
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
  staging host (below). `--strict` accepts only these.
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
| infra | `/health/live`, `/health/ready`; `/health/deep` with all seven components (OpenSearch yellow passes: a single node cannot place replicas; anything else not healthy fails); `/metrics` 401 without `METRICS_SCRAPE_TOKEN`, Prometheus text with it; through nginx: `/health` and `/api/health` 200, `/api/metrics` 404; `backup.env` present and sets `BACKUP_DIR` |
| platform-core | a user for each role; Student and Observer refused staff routes (403: audit log, users, booking, ranges, exercises), instructor refused AI config; tenant scoping (every role in the admin's tenant, rows carry it, foreign ids 404); the audit log records the exercise's create and start; demo accounts' subjects match Keycloak (needs the admin credential) |
| web-shell | the Angular shell served through nginx for `/`, `/dashboard`, `/learning/courses`; for 17 main routes, the API endpoint each page loads answers each role as `app/rbac.py` says (50 role checks) |
| content | all shipped course files (`content/courses/*.yaml`, 44) have their catalogue course with modules; every shipped template, scenario and Sigma rule is loaded; the VM image catalogue is not empty |
| curriculum | QSP qualifications with performance objectives, learning paths, developmental progression, the curriculum map; a Student's own map |
| lms | the participant Student enrolled on C103 sits its quizzed module's quiz (answers hidden from them), passes, and the module progress and the PO on their curriculum map show completed; cmi5: on an accepted release of a published course, the Student launches an AU, fetches the token once (a second fetch is refused), reads `LMS.LaunchData` and learner preferences, sends `initialized` and `terminated` through TrueNorth's LRS endpoint, and the statement is read back from the LRS; each registered Moodle platform is reachable and its results list answers |
| scenario-engine | the shipped `incident-response-drill` validates and its exercise (the installer's demo exercise, or a pending `acceptance-plan-…`) carries exactly the scenario's objectives and timeline; `acceptance-exercise-<tag>` starts, **worker-scenario** fires its inject (recorded `fired`), it stays running after the timeline (real backend), pauses (a second pause 409) and resumes |
| telemetry | three events indexed into a range, MITRE-tagged, found by administrator and Observer through the search API; a query outside the grammar 422; Students get 404 searching a range they are not on and 403 writing telemetry |
| detection | a draft booking ties the exercise to C103 (the roster); a Student not enrolled there is refused (403); the participant's detection query is credited (`achieved`, score 40/50, objective achieved, `on_target` hidden from them); a repeat is refused 409 |
| reporting-aar | complete the exercise (twice: 409), generate the AAR (HTML names the exercise, objectives and timeline), the PDF is a real PDF, a Student may read the AAR, a missing one is 404 |
| ai-arc2 | `/ai/detection-draft` returns text from the configured AI orchestrator; `/arc2/status` enabled and runs listed |
| ranges-labs | template validation accepts a good template and refuses a broken one; a range is created (recording the backend), a document stored in and read back from MinIO, the range deleted; a Student not enrolled on a course is refused a lab (403); staff list lab sessions |
| provisioners | the registered vSphere connection (or a temporary one, `--vsphere-url`) passes its connection test and discovery finds hosts; an instructor may not test it; the inventory summary |
| scheduling | capacity and a one-VM fit check; a booking 45 days out is created, listed, readable by an Observer, refused to a Student, and cancelled |
| noise-engine | presets; on a range from a noise-enabled template: not configured at first, a dry-run deploy plans the agent and registers nothing, the profile enables at level 100 and stops at 0; a template without a `noise:` block 409; a Student 403 |
| greyspace | corpus `t0` available; a range from the shipped Greyspace template reads its block, attaches it (`configured`, no problems), the config plans DNS, resolver and web farm, an unknown site pack 422, detach |
| wiki-helpdesk | a wiki space and published page (a Student reads it); a ticket assigned to the instructor reaches the instructor's inbox, is marked read, is not in the actor's inbox and is 404 to anyone else |

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
  rendering and sign-in through Keycloak are the Playwright lane's (`e2e`).
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
the quiz attempts and statements it made. Anything cleanup could not remove is printed
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
