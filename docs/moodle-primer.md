# Moodle primer for TrueNorth

What TrueNorth needs to know about Moodle to run it as an on-prem **course farm**. The
TrueNorth LMS is the source of truth; Moodle is the delivery surface. Facts marked
**verified** were run against the local instance (`truenorth-moodle-1`, Moodle **5.2.3**,
Build 20260914, PHP 8.3) between 2026-09-30 and 2026-10-05. The rest come from Moodle's
documentation and are cited.

Plan of record: structure flows TrueNorth → Moodle, results flow Moodle → TrueNorth. A
course can mix Moodle activities (pages, quizzes, files) with range VMs, which are LTI
links into TrueNorth. See `docs/moodle-integration.md`.

> Provenance: written on the Moodle farm branch (`claude/moodle-integration-courses-8bc905`)
> and brought onto the stage-4 line on 2026-10-07. That branch's own course projector
> (`app/moodle_sync.py`) was **not** brought over; courses reach Moodle through release
> publishing (`app/course_publishing`), described below.

## How Moodle is organised

| Moodle | Meaning | TrueNorth counterpart |
|---|---|---|
| Category | Folder of courses, nestable | Qualification (NQual) |
| Course | Container with its own enrolments, gradebook, completion | `Course` |
| Section | Ordered group inside a course ("Topic 1") | `CourseModule` |
| Course module (cm) | One activity instance (`mod_page`, `mod_quiz`, `mod_lti`, …) in a section | `ModuleContent` |
| Enrolment instance | How people get into a course (manual, cohort, self, meta) | `Enrollment` |
| Role | student / editingteacher / teacher, assigned per course context | `UserRole` |
| Cohort | Site-wide group of users; can auto-enrol into courses | unit / section |
| `idnumber` | Free external-ID field on category, course, cm, user, cohort | **the join key: TrueNorth UUIDs** |

**Verified:** `idnumber` is stored on courses, categories, course modules and users. The
REST call `core_course_get_contents` doesn't return the cm `idnumber`, so read it through
`local_truenorth` or `core_course_get_course_module`.

## Web services (REST)

- Endpoint: `POST <wwwroot>/webservice/rest/server.php` with `wstoken`, `wsfunction` and
  `moodlewsrestformat=json`. Parameters are PHP-style arrays (`courses[0][fullname]=…`).
- Off by default. `infra/platform/moodle/bootstrap/truenorth_setup.php` switches on web
  services and REST and creates the `truenorth_sync` external service.
- Errors come back as HTTP 200 with `{"exception", "errorcode", "message"}`, so check the
  body, not the status. **Verified:** a duplicate shortname returns `errorcode: shortnametaken`.

Core cannot name a section or create an activity (MDL-37083, still open), so TrueNorth
ships its own plugin, `local_truenorth`, which calls Moodle's internal
`course_update_section()` and `add_moduleinfo()`. TrueNorth stores no Moodle web-service
token: it talks to the plugin with signed tickets (below).

## LTI 1.3: range VMs inside a Moodle course

Moodle's core **External tool** (`mod_lti`) is an LTI Advantage *platform*: launches,
Deep Linking, AGS (grades) and NRPS (rosters). TrueNorth is the *tool*.

Register TrueNorth once per Moodle with
`infra/platform/moodle/bootstrap/truenorth_lti_tool.php --tool=<TN API URL> --publickey=<pem>`
(the farm hook does this on every start). Moodle uses the **tool type id as the
deployment id**.

**Verified** end to end on 2026-09-30: a student opened the course, clicked the range
activity, TrueNorth validated the launch as the existing TrueNorth user and redirected to
the exercise, then `POST /lti/grades` put 85/100 in Moodle's gradebook.

Four facts that decide how the farm is wired:

1. **Moodle identifies the student by `idnumber` on every launch.** `lti_build_request`
   sends `lis_person_sourcedid = $USER->idnumber` unconditionally. Set each Moodle user's
   `idnumber` to the TrueNorth user UUID (SSO does) and every launch names the TrueNorth
   user directly.
2. **Moodle answers only on its wwwroot host.** Any other `Host` header gets a 303 to
   `wwwroot`. So TrueNorth registers the *public* URLs and `lti13.platform_route()` sends
   its server-side calls (JWKS, token, AGS) to `ExternalPlatform.base_url` (the internal
   address, e.g. `http://moodle-<node>:8080`) with the public `Host`. Only URLs on the
   issuer's own origin are rerouted. The token `aud` stays the public token URL, which is
   what Moodle checks. Publishing (`moodle_backends/local_truenorth.py`) does the same.
3. **Moodle won't call private addresses.** Default `curlsecurityblockedhosts` blocks
   `10/8`, `172.16/12`, `192.168/16` and localhost, and `curlsecurityallowedport` allows
   only 80 and 443. A tool keyset URL on the internal network fails. So the tool is
   registered with **keytype `RSA_KEY` and TrueNorth's public key**, and Moodle never
   calls TrueNorth. Nodes fetch that key from `GET /lti/public-key.pem` when they start.
4. **AGS works with the per-launch lineitem.** Moodle issues
   `…/mod/lti/services.php/<course>/lineitems/<id>/lineitem?type_id=<type>` and accepts
   scores after a `client_credentials` token from `token.php`.

## Single sign-on: Students never log in to Moodle

Decided 2026-10-05: no separate Moodle login. A Student who can see a course in
TrueNorth clicks **Open in Moodle** on the course page and lands in it, signed in.
**Verified** end to end for staff (as teacher) and a Student on the farm branch.

```
TrueNorth app ──POST /integrations/moodle/sso {course_id}──▶ TrueNorth API
   ◀── {action: <moodle>/local/truenorth/sso.php, token} ──  (60 s, single use, RS256)
browser ──POST token──▶ local_truenorth/sso.php ──▶ verify ▶ find/create user ▶ enrol ▶ course
```

- **TrueNorth decides access** (`app/moodle_sso.py`). Students need an active
  enrolment (enrolled, in progress or completed) in the TrueNorth course. Staff
  (`learning_record:write`) enter as Moodle teachers. The ticket is addressed (`aud`) to
  the caller's own tenant's Moodle. The `course` claim is the TrueNorth course id, which
  is the idnumber publishing gives the live Moodle course.
- **Moodle only checks the ticket** (`local_truenorth\ticket`, `typ` = "sso"). It verifies
  the signature against TrueNorth's public key, plus `iss`, `aud` = its wwwroot, a
  lifetime of at most 120 s, and a `jti` that hasn't been used (unique index). It then
  finds the user by `idnumber` = TrueNorth UUID or creates `tn-<uuid>` with a password
  nobody knows, and enrols them through the manual plugin. An existing account that only
  shares the email is **never taken over**. A ticket sent by GET is refused.
- **No outbound calls from Moodle.** This is why it isn't Keycloak through Moodle's
  `auth_oauth2`: that needs HTTPS-only IdP endpoints, a CA every node trusts, and relaxing
  `curlsecurityblockedhosts`.
- **Pitfall found:** don't call `require_logout()` before `complete_user_login()`. It
  closes the session, so the new login doesn't stick.
- **Still open:** signing out of TrueNorth doesn't end the Moodle session; a course
  that has not been published to that Moodle shows "not available".

## Courses: accepted releases are published into Moodle

Courses reach Moodle as **releases** (`app/course_releases`, ARC² bundles), published by
`app/course_publishing` through the `moodle_backends` adapter (`local_truenorth`):

- `POST /course-releases/{release_id}/publications {"platform_id": …}` (needs
  `course:release`) stages the whole course hidden as `tn-stage:<release>`, verifies it,
  then converges the live course (idnumber = TrueNorth course id) and makes it visible.
  Students keep the previous release until activation.
- Activity idnumbers (`tn:<module>:page:NN`, `…:file:<hash>`, `…:quiz:<hash>`, `…:lab`)
  depend on the module, not the release, so a new release converges the same activities.
  Anything an instructor adds in Moodle is never touched.
- **Auth:** a ticket with `typ` = "sync", signed with the LTI tool key, bound to the body,
  and naming the tenant (`tid`); a node refuses tickets for any tenant but its own
  (`TN_TENANT_ID`). An SSO ticket is rejected here and vice versa.
- **CI:** `tests/integration/test_moodle_publish.py` runs this against a disposable Moodle
  (`infra/platform/docker/compose.moodle-test.yml`, the `moodle` job in CI).

## The farm: one command per unit

```bash
scripts/moodle-farm.sh add <node> --port <public port> [--site-url <url>]
scripts/moodle-farm.sh status
scripts/moodle-farm.sh remove <node> [--purge]
```

- **Image** `truenorth/moodle:5.2.3-tn` (`infra/platform/moodle/Dockerfile`): upstream
  Moodle plus `local_truenorth`, the bootstrap scripts and `truenorth.scss`. Nothing is
  downloaded when a node starts, so it works air-gapped.
- **Hooks** run on every start, in name order: `016-truenorth-plugin.sh` refreshes the
  plugin before install/upgrade; `04-truenorth-bootstrap.sh` fetches TrueNorth's key from
  `GET /lti/public-key.pem` (keeping the last good copy if TrueNorth is down), sets the
  sign-in and sync key and the node's tenant, registers the LTI tool and applies the
  branding, and writes `moodledata/truenorth-registration.json`; `99-public-port.sh`
  reports `MOODLE_PUBLIC_PORT`. A key rotation reaches each node on its next restart.
- **Node** (`compose.moodle-node.yml`): its own compose project `tn-moodle-<node>` with
  its own Postgres on a private network. Moodle joins TrueNorth's network as
  `moodle-<node>`, the `base_url` TrueNorth uses for server-side calls.
- **Script:** generates the database and admin passwords once into
  `~/.truenorth/moodle-farm/<node>.env` (mode 600), takes the tenant from
  `GET /auth/me` for `TN_ADMIN_TOKEN` (and stops if `TN_TENANT_ID` disagrees), then
  registers or updates platform `moodle-<node>` in that tenant. It does not push
  courses: publish releases to the new platform. `remove` marks the platform inactive;
  data stays unless `--purge`.
- **Not yet:** production hostnames and TLS (pass `--site-url https://…` behind nginx);
  one tenant should have one Moodle (SSO picks the oldest active one).

## Multi-tenancy, which decides the farm's shape

Open-source Moodle has no tenants. Multi-tenancy is a **Moodle Workplace** feature, which
is paid and sold only through Moodle partners
([docs](https://docs.moodle.org/502/en/Multi-tenancy)). The farm is therefore **one Moodle
instance per TrueNorth tenant**, each with its own database and moodledata, registered as
that tenant's `ExternalPlatform`.

## cmi5

Packaging only for now: see `docs/cmi5-packaging-decision.md`. Plugins considered for
later:

| Plugin | Fit |
|---|---|
| [ByLightSDC `mod_cmi5`](https://github.com/ByLightSDC/moodle-mod_cmi5) | GPLv3, Moodle 4.1+, built-in LRS, forward or LRS-only mode, backup/restore, gradebook. **Fits an air-gapped farm.** Not yet tested on 5.2 |
| [adlnet `mod_cmi5launch`](https://github.com/adlnet/Moodle-mod_cmi5launch) | Needs an external CATAPULT player plus an LRS. Poor fit |

## Moodle 5.x notes

- 5.1+ serves only `public/`, and `$CFG->dirroot` is `/var/www/html/public`. `config.php`
  and `admin/cli/*.php` sit above it. **Verified** on the local image.
- PHP 8.2+ is required, and 5.1+ won't start without `vendor/`. Plugins must be baked into
  the image for air-gap.

## Local harness

```bash
docker compose -p truenorth -f infra/platform/docker/compose.dev.yml -f infra/platform/docker/compose.moodle.yml up -d moodle-db moodle
```

UI at `http://localhost:8083`; inside the compose network Moodle is `http://moodle:8080`.
