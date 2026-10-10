# Moodle integration (LTI 1.3 now, cmi5 next)

> **Update 2026-10-05:** course creation in Moodle is now decided and built: accepted course
> releases are published into Moodle by the `local_truenorth` plugin through a staged,
> verified job, with native Moodle quizzes and LTI lab links. This supersedes "Custom Moodle
> PHP plugin? Not now" below. See `docs/adr/0004-moodle-course-publication.md`.

> **Status:** design. The security fixes it depends on have been made (commit `25b4a89`). The
> integration itself isn't built. Written 2026-09-23.
>
> **Reference:** `docs/xAPI CMI5 Reference/xapi-cmi5-kb/` (the owner's knowledge pack). This doc
> cites it by section (for example "KB §9.3") instead of repeating it. Check the pack's §19
> (confidence and gaps) before quoting anything.

## Decisions

| Question | Decision |
|---|---|
| Who is the learner-facing LMS? | **Moodle.** TrueNorth is the *tool/content provider*. |
| Phase 1 | **LTI 1.3**: launch into TrueNorth from Moodle, and send grades back to Moodle's gradebook. Moodle core supports it through "External tool", with no plugin. TrueNorth's side is ~80% built. |
| Phase 2 | **cmi5**: TrueNorth quizzes and range exercises become cmi5 Assignable Units (AUs), launched by a Moodle cmi5 activity plugin. TrueNorth exports a `cmi5.xml` per course. |
| Custom Moodle PHP plugin? | **Not now.** Revisit only if catalogue sync is needed (see Open decisions). |
| Record of authority | **MITE**, unchanged. The LMS and LRS never mark a qualification granted (`truenorth-content-pack/truenorth-content/lms/xapi_mapping.md`). |
| LRS | **SQL LRS (`yetanalytics/lrsql`)**, already in the dev and prod compose files, on PostgreSQL in prod (KB §11.2). |

## Architecture

```mermaid
flowchart LR
    L[Learner] -->|browses course| M[Moodle<br/>LMS]
    M -->|Phase 1: LTI 1.3 launch<br/>OIDC + signed id_token| T[TrueNorth<br/>tool / AU provider]
    T -->|AGS score| M
    M -->|Phase 2: cmi5 launch<br/>endpoint, fetch, actor,<br/>registration, activityId| T
    T -->|cmi5 + range statements| R[(LRS<br/>lrsql)]
    M -. optional logstore_xapi .-> R
    R -->|export only| X[MITE<br/>record of authority]
```

The whole chain is on-prem. The content pack's operating rules forbid calling any external or
cloud API, and forbid sending scenario data off the box. So Moodle, the LRS and TrueNorth all run
inside the same boundary (KB §13.7).

## Mapping

| TrueNorth | Moodle | cmi5 |
|---|---|---|
| Qualification (NQual) / programme | Course, or a course category | Course |
| `Course` | Course | Course (`cmi5.xml`) |
| `CourseModule` (delivers one PO) | Activity in a section | Block or AU |
| `Quiz`, assessment `Exercise` | External-tool activity (P1) / cmi5 activity (P2) | AU, `masteryScore = CourseModule.pass_threshold / 100` |
| `Enrollment` | Enrolment | Registration (learner × course, KB §13.3) |
| AGS line item | Gradebook item | — |
| `mite_course_code` + `po_code` | — | Publisher activity ID `https://ccoe.forces.gc.ca/xapi/mite/<MITE_CODE>/po/<PO_ID>` (`xapi_mapping.md`) |

`qsp_progress.py` already rolls EO → PO → Module → NQual up, as `xapi_mapping.md` describes.
A PO counts as met when its module's progress reaches `pass_threshold`. That is the same rule a
cmi5 `moveOn` of `Passed` expresses, so the two agree without new logic.

## What already exists

| Piece | Where | State |
|---|---|---|
| LTI 1.3 tool: OIDC login, JWKS, launch validation, Deep Linking, AGS push | `control-plane/api/app/lti13.py`, `routers/integrations.py` (`/lti/*`) | Built. Hardened and tested in `25b4a89` |
| Platform registry and learning records | `ExternalPlatform`, `ExternalActivity`, `LTILaunch`, `LTINonce`, `LTIToolKey` (`models.py`) | Built |
| xAPI emitter and pluggable LRS backend | `app/xapi.py`, `app/lms/` (`xapi_lrs`, `null`) | Built. **Not cmi5-conformant** (see below) |
| Local Moodle for testing | `infra/platform/docker/compose.moodle.yml` (Moodle 4.4 on :8083) | Built |
| Quiz export to Moodle | `GET /quizzes/{id}/export?format=gift\|moodlexml` | Built. The UI downloads it through `ApiService` (bearer token sent) |
| Results back from Moodle | `app/moodle_results`, `local_truenorth` `pull_results` | Built 2026-10-09: completions and quiz grades signed by the Moodle's LTI key, recorded on enrolments, progress and quiz attempts (`docs/runbooks/moodle.md` section 4) |
| cmi5 AU with no dependencies, tested 9/9 | KB `au/cmi5-au.js` | Reference code to adopt |
| LRS smoke test, cmi5 session simulator | KB `scripts/lrs-smoke-test.sh`, `scripts/cmi5-session-sim.sh` | Reference tooling |

### Security prerequisites (done: `25b4a89`)

Any integration would have widened each of these holes. All are closed, with negative tests
(`tests/api/test_integrations_authz.py`, `tests/api/test_lti13.py`):

- **Platform registration was open to any logged-in user.** Anyone could register a platform or repoint its JWKS URL, and that URL decides who may sign users in. It now needs `integration:write`, which only admins hold.
- **The LTI launch trusted the token's own audience** when no client id was registered. It also never checked `deployment_id`. Both must now come from the registration.
- **`_jit_user` matched the platform-asserted email across tenants.** Email is globally unique, so a registered platform could sign in as any user, admins included. Matching is now restricted to the platform's own tenant; any other match is refused.
- **Learning records had no tenant predicate.** Enrolments, progress, transcripts, external activities and `POST /lti/grades` now follow one rule: your own record always; anyone else's needs `learning_record:read` or `learning_record:write`, *and* that person must be in your tenant.
- **`lti_auth_login_url` was missing from the platform schemas**, so a Moodle platform could never start a launch.

## Phase 1: LTI 1.3

### Configuration

In Moodle, go to *Site administration → Plugins → External tool → Manage tools*, choose
**LTI 1.3**, and enter the tool URLs. The comments in `compose.moodle.yml` list them:

| Moodle field | TrueNorth URL |
|---|---|
| Tool URL | `<api>/lti/launch` |
| Initiate login URL | `<api>/lti/login` |
| Public keyset URL | `<api>/lti/jwks` |
| Redirection URI(s) | `<api>/lti/launch` |
| Deep Linking | enabled, same launch URL |

Then register Moodle in TrueNorth as an **admin**, using the values Moodle shows once the tool
is activated:

```http
POST /integrations/platforms
{
  "name": "Moodle", "slug": "moodle", "platform_type": "moodle", "auth_type": "lti13",
  "base_url": "https://moodle.example",
  "lti_issuer": "https://moodle.example",
  "lti_client_id": "<client id from Moodle>",
  "lti_deployment_id": "<deployment id from Moodle>",
  "lti_auth_login_url": "https://moodle.example/mod/lti/auth.php",
  "lti_token_url": "https://moodle.example/mod/lti/token.php",
  "lti_jwks_url": "https://moodle.example/mod/lti/certs.php"
}
```

Launches are refused unless `lti_client_id` and `lti_deployment_id` are both set.

**The state cookie (login CSRF).** `/lti/login` sets an HttpOnly cookie
`tn_lti_state_<hash of the state>` on the browser that began the login (`Secure`,
`SameSite=None`, 10 minutes), and `/lti/launch` refuses (401) a launch whose browser does
not hold the cookie for its state. One cookie per launch, so launches begun in two tabs
both complete; each is cleared when its launch succeeds. Two caveats:

- the cookie is `Secure`, so TrueNorth must be served over **https**: on plain http the
  browser drops it and every launch is refused;
- with the tool opened **in an iframe** inside Moodle, the cookie is third-party, and
  browsers that block third-party cookies (Safari, Chrome with tracking protection) drop
  it. Open the tool in a new window (Moodle's *Launch container: New window*), or, as a
  last resort, set `LTI_REQUIRE_STATE_COOKIE=false` on the api, which turns this
  protection off: an id_token and state from the attacker's own LMS account could then
  sign a victim's browser in as the attacker.

> Moodle's menu paths and URL names above come from general knowledge of Moodle 4.x, not from
> the reference pack (which covers Moodle only as `logstore_xapi`). Check them against the
> `compose.moodle.yml` instance before anyone relies on them.

### Gaps to close before Phase 1 works end to end

Status 2026-10-09: #1, #3, #4, #5, #6 and #7 are closed (branch `claude/moodle-loop`);
#2 follows from #1.

| # | Gap | Evidence | Fix |
|---|---|---|---|
| 1 | **An LTI-created user can't sign in to the web app.** `/lti/launch` JIT-creates a user whose `keycloak_id` is `lti:<platform>:<sub>`, then redirects into an SPA that Keycloak guards. That user has no Keycloak identity. | `routers/integrations.py` `_jit_user`, `lti_launch` | **Closed: session hand-off** (`app/lti_identity/session.py`). The launch stores a single-use, two-minute code (hash only), bound to an HttpOnly `SameSite=Lax` cookie on the launching browser, and redirects to `/lti/session#code=…`; the SPA exchanges it at `POST /lti/session` for a TrueNorth-signed session token (`typ` `lti-session`, at most `LTI_SESSION_SECONDS`, 2 h, not renewable). `get_token_identity` accepts it and re-checks the account on every request: an active Student created by an LTI launch, from a platform still registered and active. Staff, and Students with a Keycloak sign-in, never get one; nor does a launch matched to an account by its asserted email (only the account this very platform and LMS subject created, `lti:<platform>:<sub>`). Keycloak federation was not chosen: signing a credential-less Keycloak user in needs impersonation or token exchange, a wider privilege than the API's `manage-users` service account has. |
| 2 | **AGS passback identifies the learner by `users.id`**, taken from the launch. It only reaches the right gradebook row if the person who submits is the same user row. | `lti13.push_score_for_resource` | This falls out of #1. Once the launched user is the one who submits, the ids agree. |
| 3 | **Launch redirects go to `/training?quiz=` and `/exercises?exercise=`.** `/training` is now a redirect that drops `quiz=`; only `/quiz-player?quiz=` reads it. | `lti_launch` target URLs; `app.routes.ts` | **Closed:** quizzes land on `/quiz-player?quiz=<id>&lti=1`. Course launches already worked: `/training?course=` lands on the course page (commit `c5f4a5b`). |
| 4 | **Exercises aren't attributable to a trainee.** `Exercise` has no learner column, so exercise statements and grades name the instructor who clicked. | `models.py` `Exercise`; `routers/exercises.py` emit sites | **Closed for launches:** an exercise launch records `exercise_learners` (exercise, Student, platform, resource link; one per Student per run; another tenant's exercise is 404) and lands on the Student's page `/exercises/<id>`. Credit and grades still come from the Student's own detections (ADR 0005, `exercise_completion.participants`), not from launching. |
| 5 | **The web UI advertises the wrong tool URLs**: `/api/v1/lti/*` (routes have no `/v1`) and a nonexistent `/lti/deeplink`. It also offers `platform_type` `lti_generic`, which the schema rejects. | `features/integrations/integrations.component.ts:91,170-174` | **Closed:** the UI shows `GET /integrations/lti/tool-config` (built from `LTI_TOOL_BASE_URL`: tool, login, redirection, keyset; deep linking at the tool URL). `lti_generic` is gone. |
| 6 | **Prod sets no `LTI_TOOL_BASE_URL` or `LTI_WEB_BASE_URL`**, so both fall back to localhost. The dev defaults also disagree: `.env.example` and the Moodle harness's tool URLs use :8980, but `compose.dev.yml` exposes the API on :8081. | `infra/platform/docker/compose.prod.yml`; `.env.example` | **Closed:** one source, group_vars `tn_lti_tool_base_url` (`https://<fqdn>/api`) and `tn_lti_web_base_url` (`https://<fqdn>`), rendered into `.env.production` and passed by `compose.prod.yml` to the api; the Moodle role registers its tool at the same base (`tn_moodle_tool_url`); the chart sets `<origin>/api` and `<origin>`. Dev uses :8081 throughout (`compose.windows-ports.yml` still maps :8980 for Windows hosts; set `LTI_TOOL_BASE_URL` to match there). Contract: `tests/contracts/test_lti_base_urls.py`. |
| 7 | **The quiz export link sends no bearer token**, so it fails once auth is on. | `curriculum-forge.component.ts` (`<a href>` to `/quizzes/{id}/export`) | **Closed:** downloaded through `ApiService.exportQuiz` as a blob. |

### Staff deep linking (2026-10-09)

The email claim is the LMS's assertion, so it never makes a launch a staff account
(`_jit_user`). Instead a staff member links their LMS account once, explicitly
(`app/lti_identity/links.py`):

1. A **deep-linking** launch whose email is a staff account's in the platform's tenant
   gets a page, not a 403: "link this account". It stores a request (platform, LMS
   subject, a hash of the asserted email; single use; ten minutes) bound to an HttpOnly
   `SameSite=Lax` cookie on that browser. The binding is required: with
   `LTI_REQUIRE_STATE_COOKIE=false` no link is offered (still a 403), since an unbound code
   could be sent to a staff member to confirm. A resource launch asserting a staff email
   is still a 403.
2. The button opens `/lti/link#code=…` in TrueNorth. The staff member signs in as
   themselves (Keycloak; an LTI session cannot link), sees which LMS account and site it
   is (`POST /lti/links/preview`), and confirms (`POST /lti/links/confirm`).
3. TrueNorth binds `(platform, sub) -> user` (`lti_user_links`) only if: the code is live
   and from this browser; the platform is the caller's tenant's and active; the caller is
   staff; the email the LMS asserted is the caller's own; and neither side is linked on
   that platform already (409). Otherwise 403/404/410 and nothing changes.
4. From then on that LMS account's launches are that staff member: deep linking shows the
   picker. Staff are never handed an LTI session (they use Keycloak). `GET /lti/links`
   lists your links; `DELETE /lti/links/{id}` removes one (the holder, or an
   `integration:write` admin of the tenant). Deregistering the platform removes its links.

Students are unchanged: their email links an existing Student account, as before. The
threat cases (email collision, another staff member or a Student confirming, another
tenant, another browser, a replayed or expired code, the same subject on another tenant's
platform) are tests in `tests/api/test_lti_staff_link.py`.

### cmi5 modules over LTI (2026-10-10)

A module of a released course (a cmi5 AU, `docs/cmi5.md`) is added to a Moodle course as an
**External tool** activity, chosen by deep linking. TrueNorth is the AU's cmi5 LMS; Moodle
launches it and keeps the grade.

Prerequisites: the TrueNorth Range tool registered in Moodle as above (the farm image does it,
`infra/platform/moodle/bootstrap/truenorth_lti_tool.php`: deep linking on, *Accept grades*
always, *IMS LTI Assignment and Grade Services* "Use this service for grade sync and column
management"); the platform registered in TrueNorth with `lti_token_url`; the course published
in TrueNorth with an accepted release; cmi5 configured (`CMI5_LRS_AUTH`).

1. As the teacher, in the Moodle course: *Add an activity or resource → TrueNorth Range →
   Select content*. Moodle sends a deep-linking launch; TrueNorth shows its picker. Under
   the quizzes and courses it lists each module of the tenant's published courses as
   `<release title>: <module title>` (the newest accepted release of each, up to 20
   courses). Tick the modules (one activity each) and *Add selected to course*.
   A teacher whose Moodle email belongs to a TrueNorth staff account links it once first
   (*Staff deep linking*, above).
2. Moodle creates the activities with the custom parameter `resource=cmi5:<release>:<n>` and
   a grade item out of 100. Keep *Launch container: New window* (the state cookie).
3. A Student opens the activity. TrueNorth checks the module (this tenant, published,
   accepted release, the AU exists) and that **the Student is enrolled in the course in
   TrueNorth on that release**: the launch never enrols (403 if not enrolled). Enrol in
   TrueNorth; the farm puts the Student in the Moodle course. A Student whose account the
   launch created is handed a session (gap #1); the module opens and starts at once.
4. When the Student passes or fails the module's quiz, TrueNorth sends its own mark (never
   the score the browser reported) to the activity's grade item over AGS, and resends it
   if Moodle was unreachable. A module with no quiz reports completion without a grade.

Details, refusals and the Score sent: `docs/cmi5.md`, "Moodle over LTI 1.3". When a new
release of the course is accepted, Students already enrolled stay on theirs (their launch
of an activity linked to another release is 409); add the new release's modules as new
activities for new Students.

Tested against a real Moodle 5.2.3: `tests/integration/test_moodle_cmi5_lti.py` (CI job
`moodle`).

### Acceptance

1. `compose.moodle.yml` up; register Moodle through the API with `lti_auth_login_url`. A student token gets 403 on the same call.
2. From Moodle, Deep Link a published quiz into a course.
3. As a Moodle learner: launch, sign in once through the hand-off, take the quiz. The score appears in Moodle's gradebook.
4. Replay the same `id_token`: refused. Launch from a second Moodle deployment that isn't registered: refused.

## Phase 2: cmi5

> **Built (2026-10-09):** 2a (served `cmi5.xml` per release), 2b (the AU runtime, `/au/...`),
> 2d (conformant xAPI) and 2e (LRS configuration), plus TrueNorth's own LMS side. Status,
> routes and limits: [`docs/cmi5.md`](cmi5.md). 2c (range exercises as AUs) is not built.
> (Deferred on 2026-10-07 in `docs/cmi5-packaging-decision.md`; that is superseded.)

TrueNorth becomes a cmi5 **content provider**. Moodle and a cmi5 activity plugin act as the
LMS: they launch AUs and issue `launched` / `satisfied` / `abandoned` / `waived`. TrueNorth's AUs
issue `initialized` / `completed` / `passed` / `failed` / `terminated` (KB §9.3, §9.9). The
sequence is in KB §9.3.

### 2a. Course structure export

`GET /courses/{id}/cmi5.xml` should return a Quartz course structure (KB §9.14, and
`examples/cmi5.xml` in the pack):

- One `<course>`. One `<block>` per module grouping if needed. One `<au>` per quiz or
  assessment exercise.
- **Every `<url>` fully qualified.** A bare `cmi5.xml` with no ZIP must use fully qualified URLs
  (cmi5 spec §14).
- `moveOn="Passed"` for assessments; `masteryScore = pass_threshold / 100`; `launchMethod="AnyWindow"`.
- Publisher IDs under the MITE scheme, stable across minor revisions (KB §13.2). The LMS mints
  its own runtime activity IDs; never use the publisher ID as `object.id` at runtime.
- Validate the output against `CourseStructure.xsd` in a test.

### 2b. AU runtime in the SPA

Add a route, for example `/au/:kind/:id`, that:

1. reads `endpoint`, `fetch`, `actor`, `registration` and `activityId` from the query (KB §9.4);
2. POSTs **once** to `fetch` for the auth token (KB §9.5), and never stores it beyond the session;
3. GETs `LMS.LaunchData` for `launchMode`, `masteryScore`, `moveOn` and `returnURL` (KB §9.6);
4. sends `initialized`, then runs the quiz or exercise;
5. sends `completed` / `passed` / `failed` with `result.duration` and the `masteryscore` extension, then `terminated`, then returns to `returnURL` (KB §9.10–9.12).

**Adopt `au/cmi5-au.js`** from the pack rather than writing a client or adding
`@rusticisoftware/cmi5`. It has no dependencies and is already tested, and `docs/AGENTS.md`
asks for a reason before adding any dependency. In Browse or Review mode, send only
`initialized` and `terminated`.

### 2c. Range exercises

This follows KB §17: the AU is the launch point, the range backend reports outcomes, and the AU
(holding the session token) sends `completed`/`passed`. Use one registration per trainee per
exercise run. Range-side statements carry the cmi5 context template. This depends on gap #4
(exercise attribution).

### 2d. Make `app/xapi.py` conformant

> **Done (2026-10-09):** account actor (`users.id`, no email), IRIs under `XAPI_IRI_BASE`,
> `context.registration` on every learning statement, course-locale language maps,
> per-activity pass marks, durations, and the legacy-identity migration with a
> leave/re-issue/void policy for statements already in the LRS. See
> [`docs/xapi-conformance.md`](xapi-conformance.md). The table below is the original gap list.
> The publisher IDs still follow ARC²'s `https://ccoe.forces.gc.ca/xapi/arc2/...` scheme,
> not the MITE scheme, until Standards binds courses to POs.

| Before (`app/xapi.py`) | Required |
|---|---|
| `actor.mbox = mailto:<email>` plus `name` (line 35) | `account {homePage: <TrueNorth identity authority>, name: <users.id>}`, no name. Emails are forbidden as the cmi5 actor, and hashing them is not anonymisation (KB §13.1). |
| Object IDs `http://truenorthrange.local/...` (line 51) | The MITE scheme from `xapi_mapping.md` for publisher IDs; the LMS-issued `activityId` at runtime. |
| No `context.registration` | The launch registration, on every statement. |
| No cmi5 context | Category `…/cmi5/context/categories/cmi5`; `moveon` on `completed`/`passed`/`failed`; the `sessionid` extension (KB §9.8, §9.12). |
| No `result.duration`; success fixed at `>= 0.7` (line 127) | Duration on `completed`/`passed`/`failed`/`terminated`; pass/fail against the launch `masteryScore`. |
| `en-US` language maps (lines 43, 54, 84) | `en-CA` (and `fr-CA` where content exists). |
| Content-pack `xapi.json` templates lack `account.homePage` | Add it. xAPI rejects an `account` without it. |
| `component_version` pinned `v2.1.0` in `xapi_mapping.md` | `SP800-181r1`, as `docs/current-state.md` already corrected. |

Changing the actor is a **data migration question** as well as a code change. Existing LRS
statements identify people by email. Decide whether to leave them, or to void and re-issue them
(KB §5.10), before switching.

### 2e. LRS configuration (KB §11.2, §13.6)

- PostgreSQL-backed lrsql in prod, with **1.0.3 and 2.0.0 both enabled**. cmi5 content speaks 1.0.3.
- Per-client, least-privilege credentials: write-only for TrueNorth's server-side producers, read-only for dashboards. **No long-lived LRS credential in the browser**; the AU uses only the cmi5 per-session token.
- Retention set in LRS config, not by convention. The LRS holds personal information, so a privacy impact assessment and a retention schedule apply (KB §13.1).
- Optional: Moodle's `logstore_xapi` forwards Moodle's own events into the same LRS, giving one record across both systems.

### Acceptance

1. `scripts/lrs-smoke-test.sh` passes 14/14 against TrueNorth's lrsql with both version headers.
2. `scripts/cmi5-session-sim.sh` completes end to end. With `LMS_ONLY=1`, it launches TrueNorth's real AU.
3. CATAPULT **CTS** passes for TrueNorth's AUs, and CATAPULT **LTS** passes for the chosen Moodle cmi5 plugin (KB §11.1).
4. A trainee's quiz AU in Moodle produces `launched → initialized → passed → terminated → satisfied` under one registration, with an `account` actor and no email anywhere.

## Constraints

- **Air-gapped.** Nothing leaves the box, and the reference IRIs (`w3id.org`, `adlnet.gov`) are identifiers, not network dependencies (KB §13.7).
- **Anything mounted under `/xapi` is pre-exempted** by `tests/api/test_auth_coverage_guard.py` (`PUBLIC_PREFIXES`), on the assumption that it has its own credential. A cmi5 fetch endpoint or LRS proxy placed there **must** enforce that credential itself, because the guard will not catch it.
- **No service-account scheme exists** for Moodle to call TrueNorth's API. `IntegrationAuthType.api_key` and `oauth2` are enum values with nothing behind them.

## Open decisions

1. ~~**The LTI session hand-off (gap #1):** an exchangeable single-use token, or Keycloak federation of LTI users?~~ Decided 2026-10-09: the exchangeable single-use code (see gap #1).
2. **Which Moodle cmi5 plugin to standardise on.** Choose on CATAPULT LTS results, not the feature list.
   *2026-10-09:* neither candidate for now (ByLight `mod_cmi5` 0.2.0 breaks cmi5 MUSTs; ADL
   `mod_cmi5launch` needs the CATAPULT player per node). Moodle links into TrueNorth's own
   cmi5 launch instead: `docs/cmi5.md`, "Moodle".
3. **A custom `local_truenorth` Moodle plugin** would only be needed to sync TrueNorth's catalogue (courses, a section per PO, competencies) into Moodle automatically. Deep Linking covers placing individual activities without it.
4. **Machine-to-machine auth, if Moodle ever calls TrueNorth:** a Keycloak `client_credentials` client mapped to a tenant-scoped service user, or a new API-key scheme.
5. **Existing email-identified LRS statements** when the actor changes (see 2d).
