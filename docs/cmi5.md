# cmi5 in TrueNorth

TrueNorth plays both cmi5 roles for its released courses (2026-10-09; this supersedes the
"packaging only" decision of 2026-10-07, `docs/cmi5-packaging-decision.md`):

- **Content (AUs).** Every module of an accepted release is an assignable unit served by the
  SPA's AU runtime, `/au/releases/<release>/<index>`. Any cmi5 LMS can launch it: TrueNorth
  itself, Moodle, PCTE. `GET /cmi5/releases/{id}/cmi5.xml` hands such an LMS the course
  structure with every AU URL pointing at that runtime.
- **LMS.** TrueNorth launches its own releases for its enrolled Students: registration,
  `LMS.LaunchData`, the one-time fetch URL, an xAPI endpoint for the AU that enforces the cmi5
  rules, `launched` / `abandoned` / `waived` / `satisfied`, moveOn.

The ARC² package (`07-bundle/cmi5/` in the release, `cmi5.js` + `course.js`) is still what is
handed over as a ZIP when another LMS must host the content itself; that path is unchanged.

Code: `control-plane/api/app/cmi5/` (structure, content, rules, lms, router),
`control-plane/web/src/app/features/cmi5/` (the AU runtime, `cmi5-au.ts`, and the launcher).
Statement identity and the LRS: [`docs/xapi-conformance.md`](xapi-conformance.md).

## Routes

| Route | Who | What |
|---|---|---|
| `GET /cmi5/releases/{id}/cmi5.xml` | `course:author` | The structure for another LMS: publisher IDs, objectives, moveOn, masteryScore and launchParameters as published; every `<url>` absolute (`<web>/au/releases/{id}/{n}`); `launchMethod="OwnWindow"` (TrueNorth refuses to be framed by another origin, and its sign-in cookie is first-party). Valid against `CourseStructure.xsd` (vendored, `docs/interfaces/cmi5/`). |
| `GET /cmi5/releases/{id}/structure` | signed in, same tenant | The AUs, and the caller's own progress when they have a registration. |
| `GET /cmi5/releases/{id}/aus/{n}/content` | an enrolled Student, or `course:author` | Pages and quiz questions for the AU runtime. Never the answers. |
| `POST /cmi5/releases/{id}/aus/{n}/grade` | an enrolled Student, or `course:author` | Marks the AU's quiz on the server and records the mark: `{correct, total, scaled}` only, no per-question result. A Student gets `CMI5_GRADE_ATTEMPTS` (3) marks per AU per 24 hours; then 429. |
| `POST /cmi5/releases/{id}/aus/{n}/launch` | an enrolled Student | TrueNorth's launch: returns the AU URL with the cmi5 parameters. |
| `POST /cmi5/fetch/{secret}` | the secret | The fetch URL: the session's auth token, once. |
| `GET/POST/PUT/DELETE /cmi5/lrs/{resource}` | the session token (Basic) | The AU's xAPI endpoint. |
| `POST /cmi5/sessions/{id}/abandon` | the Student, or `learning_record:write` | Ends an open session (`abandoned`). |
| `POST /cmi5/registrations/{id}/aus/{n}/waive` | `learning_record:write` | `waived` with a reason, then moveOn. |

Without `course:author`, every route above sees only an accepted (or superseded) release of a
**published** course; a candidate, or an unpublished course, is 404, as the course catalogue
treats drafts. Content, marking and launch also need the caller's enrolment pinned to the
release (403 / 409). Another tenant's release is 404.

## TrueNorth as the LMS: one session

The registration is the Student's **enrolment**: its id is `enrollments.id`, the same value
TrueNorth's own xAPI statements carry (docs/xapi-conformance.md), pinned to the release the
enrolment is pinned to. Runtime activity IDs are TrueNorth's, never the publisher's (cmi5
8.1.5.0-3): `<XAPI_IRI_BASE>/cmi5/releases/<release>` for the course, `/block/<n>` and
`/au/<n>` under it, the same for every launch.

1. **Launch** (`POST .../launch`): abandons any session still open in the registration, then
   writes `LMS.LaunchData` and records `launched`:

   ```json
   {"contextTemplate": {"contextActivities": {"grouping": [{"objectType": "Activity",
        "id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105/au/mod_001"}]},
      "extensions": {"https://w3id.org/xapi/cmi5/context/extensions/sessionid": "b5c1…"}},
    "launchMode": "Normal", "moveOn": "Passed", "masteryScore": 0.7,
    "launchParameters": "{\"module\": \"mod_001\", \"lang\": \"en-CA\"}",
    "returnURL": "https://range.example.mil/au/releases/9a0e…"}
   ```

   ```json
   {"id": "<HMAC-derived, UUID version 8>", "actor": {"objectType": "Agent",
      "account": {"homePage": "https://range.example.mil", "name": "<users.id>"}},
    "verb": {"id": "http://adlnet.gov/expapi/verbs/launched", "display": {"en": "launched"}},
    "object": {"objectType": "Activity", "id": "https://range.example.mil/xapi/cmi5/releases/9a0e…/au/0"},
    "context": {"registration": "<enrolment id>",
      "contextActivities": {"category": [{"id": "https://w3id.org/xapi/cmi5/context/categories/cmi5"}],
                            "grouping": [{"id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105/au/mod_001"}]},
      "extensions": {"…/sessionid": "b5c1…", "…/launchmode": "Normal", "…/moveon": "Passed",
                     "…/masteryscore": 0.7, "…/launchurl": "https://range.example.mil/au/releases/9a0e…/0",
                     "…/launchparameters": "{\"module\": \"mod_001\", …}"}},
    "timestamp": "2026-10-09T14:00:00.000Z"}
   ```

   The launch URL: `<web>/au/releases/<release>/0?endpoint=<api>/cmi5/lrs/&fetch=<api>/cmi5/fetch/<secret>&actor=…&registration=…&activityId=…`.
   The launch mode is `Review` once the AU is satisfied (unless its moveOn is NotApplicable),
   else `Normal`; `Browse` and `Review` can be asked for.
2. **Fetch** (the AU, once): `{"auth-token": "…"}`; again → `{"error-code": "1", …}`; unknown
   → `"2"`. HTTP 200 either way. The secret works for 15 minutes after launch
   (`CMI5_FETCH_HOURS`) and once, and is stored hashed. It travels in the AU URL's query, so:
   the SPA takes the launch out of the address bar before its first request (kept for the tab
   in sessionStorage, so a reload resumes); `/au/` pages are served with
   `Referrer-Policy: no-referrer`; both nginx configs log the request line and Referer with
   the `fetch=` value replaced by `<redacted>`, and never log `/api/cmi5/fetch/` itself. The
   LMS's own launch response (`POST .../launch`) and a browser history entry made before the
   SPA loaded can still hold it; it is dead once used or 15 minutes old.
3. **The AU** reads `LMS.LaunchData` and `cmi5LearnerPreferences`, then sends `initialized`,
   cmi5-allowed `progressed` per page, `completed` after the last page, then submits the quiz
   to TrueNorth's marking (`POST .../grade`) and reports `passed` or `failed` with exactly that
   score (`score.scaled`/`raw`/`min`/`max`, the `masteryscore` extension), and `terminated` on Exit (or, best effort with `keepalive`, when the window
   closes), then follows `returnURL` back to the course page.
4. **moveOn**: after an accepted `completed`/`passed` (or a waiver), TrueNorth evaluates the
   AU's moveOn and records `satisfied` for every block and then the course that became
   satisfied, with the triggering session id, the runtime block/course id as the object
   (`definition.type` `…/activitytype/block` or `…/course`) and the publisher id in
   `grouping`. NotApplicable AUs count from the registration's creation.

C105 played through: `launched → initialized → progressed ×5 → completed → passed →
satisfied (block) → terminated`; with the other five modules passed or waived, a final
`satisfied (course)`.

### What the AU's endpoint enforces

Every statement is checked before anything reaches the LRS, and a refusal is HTTP 400 (403
for the forbidden kinds) with `{"error": …, "violatedReqId": "<cmi5 requirement>"}`, the
numbering ADL's CATAPULT uses. TrueNorth rejects, so it never has to void.

| Rule | Requirement |
|---|---|
| learner preferences read before any statement | 11.0.0.0-3 |
| statement id, UTC timestamp | 9.1.0.0-1, 9.7.0.0-1/-2 |
| the launch actor (account IFI), registration, sessionid; the contextTemplate followed, never overwritten | 8.1.3.0-3, 9.6.1.0-1, 9.6.3.1-4, 10.2.1.0-6/-7 |
| `initialized` first, `terminated` last, each once; nothing after `terminated` | 9.3.0.0-2/-4/-5 |
| cmi5-defined verbs only from the AU (never launched/abandoned/waived/satisfied), object = the launch activityId | 9.3.0.0-1, 8.1.5.0-6 |
| `completed` once per registration; `passed` once; `failed` never after `passed`; one of passed/failed per session | 9.3.0.0-3/-6/-7/-8 |
| result rules: completion only on completed, success only on passed/failed, score only on passed/failed (raw needs min/max), durations required, moveon category exactly on completed/passed/failed | 9.5.x, 9.6.2.2-1/-2 |
| passed at or above the masteryScore, failed below it, and the `masteryscore` extension when judged | 9.3.4.0-2, 9.3.6.0-1, 9.6.3.2-2 |
| no completed/passed/failed in Browse or Review | 10.2.2.0-2/-3 |
| `LMS.LaunchData` read-only; State and Profile only for this actor and activity; Agent Profiles (the learner preferences included) read-only | 10.2.1.0-5, 10.1.0.0-3 |
| no voiding | 6.3.0.0-1 |
| `LMS.LaunchData` never written or deleted; every State request names this registration, and a write or delete names its `stateId`; Activity Profiles and the activity definition read-only | 10.2.1.0-5, 8.1.4.0-3 |

On top of cmi5, because the same LRS holds TrueNorth's own records (refusals `TN-…`, 403
unless marked otherwise):

| Rule | Id |
|---|---|
| every statement is about the launch activity (its `activityId`, or a plain path under it: no empty, `.` or `..` segment, no `%`-encoding, `?` or `#`): an AU cannot write about a TrueNorth quiz, course or other module | `TN-SCOPE` |
| `context.contextActivities` (parent, grouping, other, category) names no TrueNorth activity (under `XAPI_IRI_BASE` or the legacy root) outside the AU's own subtree; activities elsewhere are the AU's business | `TN-SCOPE` |
| `initialized`, `completed`, `passed`, `failed`, `terminated` only as cmi5-defined statements (with the cmi5 category), so none escapes the rules above | `TN-DEFINED` |
| no verb id that is a cmi5 or LMS verb spelled differently (case, `https`, a trailing slash) | `TN-VERB` |
| a cmi5-allowed (uncategorised) statement carries no `result.success`, `result.completion` or `result.score`: only cmi5-defined statements judge | `TN-RESULT` |
| no NaN, infinity or integer beyond a double anywhere in the statement (400; NaN compares false against every mark) | `TN-NUMBER` |
| judged against a masteryScore, `passed`/`failed` carry `score.scaled` (400) | `TN-SCORE` |
| `passed`/`failed` report the score TrueNorth marked for this Student and AU since the launch | `TN-GRADE` |
| statement ids of UUID version 8 are reserved for TrueNorth's own statements | `TN-ID` |

`authority` and `stored` are the LRS's to set: an AU's values are dropped before forwarding
(the body forwarded is the one checked, re-serialised). Every AU request also re-checks that
the course is still published and the Student still enrolled on this release (two
primary-key reads); otherwise 403, whatever the token's age.

Marking (`POST …/grade`) is attempt-limited per Student and AU; the handler takes the
enrolment's row lock (a no-op `UPDATE`, which is also SQLite's write lock) before counting,
so concurrent submissions cannot both find room under the limit.

**Provenance.** AU traffic reaches the LRS with its own credential, `CMI5_LRS_AUTH` (scoped:
statements write-only, State, Agent and Activity Profiles), never the server's `LRS_AUTH`. The
LRS stamps every statement's `authority` with the credential that wrote it, so a statement an
AU wrote is always distinguishable from one TrueNorth wrote. Without `CMI5_LRS_AUTH`, or with
it equal to `LRS_AUTH`, launching answers 503 (fail closed). Mint it once on lrsql with its
admin account: `python -m app.lms.lrsql_admin` (reads `LRS_URL`, `LRS_ADMIN_USER`,
`LRS_ADMIN_PASSWORD`; `scripts/itest.sh` does this for the itest stack).

The AU's token cannot read statements back (least privilege; cmi5 does not need it) and is
dead once the session is terminated, abandoned, or 12 hours old (`CMI5_SESSION_HOURS`).

### Delivery guarantees

- TrueNorth's own statements (launched, abandoned, waived, satisfied) are written with
  `PUT /statements?statementId=` and ids that are an HMAC (key `TN_SECRETS_KEY`) of what they
  record, shaped as UUID version 8: stable for a retry, unguessable without the key, and a form
  AUs may not use. On a 409 the stored statement is read back; only the same verb, actor and
  object counts as already recorded, anything else under the id fails the operation (502).
- A launch whose `LMS.LaunchData` or `launched` the LRS refuses fails (502/503) and creates no
  session. What it already did (an abandoned session) is kept, matching the LRS.
- A `satisfied` the LRS refuses is not marked sent and is retried at the next evaluation
  (the Student's next statement or launch); the AU's own accepted statement is not failed for it.
- The AU's statements are forwarded only after every one in the request passes; the LRS's
  answer (status, body, ETag) is returned unchanged.
- An unreachable LRS is a 503, never a success.

## Launching from another LMS

`GET /cmi5/releases/{id}/cmi5.xml` (course author) gives the structure for PCTE, Moodle or
any cmi5 LMS. That LMS owns the registration, the session and the LRS; the AU runtime talks
to its endpoint with its token. Two things it needs from TrueNorth's side:

- **Sign-in and enrolment.** The AU reads the module's pages from TrueNorth, so the Student
  signs in to TrueNorth (single sign-on with the LMS where both use the same Keycloak, as the
  Moodle farm does) and must be enrolled on the release there. The query parameters survive
  the sign-in round trip.
- **Self-reported results.** TrueNorth marks the quiz (attempt-limited, recorded), but the AU
  reports passed/failed to *that* LMS's LRS, which TrueNorth does not check: there the result
  is self-reported, as it is for any content an LMS hosts. Only TrueNorth's own launches
  enforce the marked score (`TN-GRADE`).
- **Reaching the LMS's endpoint.** The production CSP allows the SPA to connect to its own
  origin and to any https port on its own host (the farm Moodle). An LMS on another host
  needs its origin added to `connect-src` in `infra/platform/nginx/snippets/security-headers.conf`,
  and its LRS must allow TrueNorth's origin in CORS.

## Moodle

**Decision (2026-10-09): no cmi5 activity plugin in the farm image.** A Moodle course reaches
a TrueNorth cmi5 module through a link into TrueNorth, and TrueNorth is the cmi5 LMS for it.

Both community plugins were installed on the farm image (`infra/platform/moodle`, Moodle
5.2.3, PostgreSQL 16) in a throwaway compose project; both install cleanly. What decided it is
how each behaves as a cmi5 LMS:

| | ByLightSDC `mod_cmi5` | adlnet `mod_cmi5launch` |
|---|---|---|
| Commit tested | `7485551cb42b39efb52ca205860a10676de673a6` (0.2.0, maturity alpha) | `9c7d90da44035839a239380fcc2e0c7aa6910a85` (1.1.0) |
| Licence | GPL-3.0-or-later | Apache-2.0 (GPLv3-compatible) |
| Moodle 5.2.3 | installs (`mod_cmi5` 2026092400) | installs (`mod_cmi5launch` 2025070116) |
| LMS side | its own, in Moodle | ADL's CATAPULT player: a separate Node service with its own MySQL, called by the plugin |
| Conformance | fails cmi5 MUSTs (below) | the player is ADL's reference prototype |
| Fit for the farm | no | no: a prototype service and database per farm node, its own tenant/token setup |

`mod_cmi5` at that commit, read in its source:

- the launch `activityId` is the AU's publisher id (`classes/launch_manager.php`, `'activityId' => $au->auid`;
  `auid` is the course structure's `id`, `classes/cmi5_package.php`): cmi5 8.1.5.0-3 says it MUST NOT be;
- the actor's account name is the Moodle user's email (`classes/xapi_statement.php`, `get_actor`),
  which TrueNorth's identity rules forbid (docs/xapi-conformance.md);
- no `launched` statement is recorded (only a Moodle event, `launch.php`);
- `satisfied` is issued for AUs as well as blocks, with the publisher id as the object, no
  `…/activitytype/block|course` type and a session id of `"LMS-generated"`
  (`classes/xapi_statement.php`, `build_satisfied_statement`).

Revisit when those are fixed upstream (it is the plugin closest to PCTE's tooling, and it is
active); then bake it into the image with an install hook pinned by commit, and run CATAPULT's
LMS test suite against it before relying on it.

**The path instead (an LTI-wrapped AU).** The Moodle activity opens
`<web>/au/releases/<release>?launch=<n>`. The launcher starts module *n* for the signed-in
Student with TrueNorth as the LMS: registration (their enrolment), `launched`, the AU, moveOn
and `satisfied`, all in TrueNorth's LRS. What works today and what does not:

| | |
|---|---|
| A Moodle **URL** resource to `<web>/au/releases/<release>?launch=<n>` | Works now. The Student arrives signed in through the farm's single sign-on; an unenrolled Student is told so and nothing launches. No grade in Moodle. |
| A Moodle **External tool** (LTI 1.3) activity | **Built (2026-10-10).** Resource kind `cmi5`, id `<release>:<n>`, picked by deep linking (one content item per AU). The launch sends the Student to `<web>/au/releases/<release>?launch=<n>&lti=1`. Below. |
| Moodle gradebook | **Built (2026-10-10).** TrueNorth's own mark goes to the activity's AGS line item when the AU reports `passed`/`failed`; `completed` reports progress with no score. Below. |

### Moodle over LTI 1.3: launch and grade (app/cmi5/lti.py, app/cmi5/ags.py)

Setting up the activity in Moodle is in `docs/moodle-integration.md`, "cmi5 modules over LTI".

**Launch.** `POST /lti/launch` with custom `resource=cmi5:<release id>:<AU index>`:

| Case | Answer |
|---|---|
| The release is the platform's tenant's, accepted or superseded, of a published course, and has that AU; the account (in the same tenant) is enrolled in the course on this release | `302` to `<web>/au/releases/<release>?launch=<n>&lti=1`. An account the LTI launch created goes through the session hand-off first (`/lti/session#code=…`, docs/moodle-integration.md gap #1); anyone else signs in as usual. The enrolment's cmi5 registration is created or reused (its id is the enrolment id), and the launch is recorded with its AGS line item. The SPA then launches the AU for the Student (`POST /cmi5/releases/{id}/aus/{n}/launch`). |
| Not enrolled, or the enrolment is withdrawn | `403`, nothing recorded. **The launch never enrols.** A `course` LTI launch does not enrol either; TrueNorth stays the source of enrolment (the farm syncs it to Moodle). The `lab` launch's auto-enrolment is not extended to cmi5. |
| Enrolled on another release of the course | `409` (the deep link names a release; relink the activity to the release Students are on) |
| Another tenant's release, an unpublished course, a candidate release, no such AU | `404`, nothing recorded |
| A malformed id (`<release>:<n>` with `n` a plain non-negative integer) | `400` |
| cmi5 not configured (`CMI5_LRS_AUTH`) | `503`, as for any cmi5 launch |

Every check runs before the launch is recorded, so a refused launch leaves no line item a
result could later be sent to.

**Deep linking.** The picker (`/lti/launch` with `LtiDeepLinkingRequest`) lists every AU of
the newest accepted releases of the tenant's published courses that have a cmi5 package (at
most 20 such releases, out of the newest 200; a release without one is skipped and not
counted, and each bundle's AU titles, or its lack of a package, are cached by blob). It sits
beside quizzes and courses. `/lti/deeplink/finish` takes at most 50 items (422 otherwise),
re-checks each `cmi5:` selection against the platform's tenant, accepts only the course's
current (accepted) release (a superseded one stays launchable for the Students pinned to
it, but is no new activity), and titles it from the release (`<release title>: <AU
title>`), ignoring any title the browser posted. Each becomes an `ltiResourceLink` with
`custom.resource = "cmi5:<release>:<n>"` and `lineItem.scoreMaximum = 100`.

**Grade pass-back (AGS).** When TrueNorth, as the AU's cmi5 LMS, accepts a `passed`,
`failed` or `completed` (cmi5-defined, so with the cmi5 category and through every rule
above), it writes the AU's result for each of the Student's gradebook cells in the same
transaction (`cmi5_ags_scores`, one row per platform, line item and the platform's user),
and sends it after the statement's response:

| AU state | Score sent |
|---|---|
| `passed` or `failed` accepted | `scoreGiven` = TrueNorth's mark x 100 (`cmi5_grades`, `POST .../grade`), `scoreMaximum` 100, `activityProgress` Completed, `gradingProgress` FullyGraded. The rules already refuse a statement whose score is not that mark (TN-GRADE); the number in the statement is never what is sent. **The best mark of the registration is sent**: a later, lower `failed` (a retake) never replaces an earlier, higher result, as moveOn never takes back a pass (and cmi5 refuses `failed` after `passed` in a registration). |
| `completed` only | No `scoreGiven`; `activityProgress` Completed, `gradingProgress` Pending. Once a mark exists it is always included, so a later `completed` never clears a grade. |

- **Which launches.** An LTI launch of this AU by an LMS account **bound to the Student**:
  the account its own launch created (`lti:<platform>:<sub>`) or one linked explicitly
  (`lti_user_links`; on a TrueNorth farm node, the account TrueNorth's sign-in made is linked
  at its first launch by its locked TrueNorth id: docs/moodle-integration.md, "Farm nodes"). An LMS account matched to the Student by the email it asserted still
  signs in as them, but its launch keeps no line item and gets no grade: any LMS account
  can assert any email, and would otherwise receive that Student's grade in its own
  gradebook cell (review of #140; the same rule now applies to
  `lti13.push_score_for_resource`, quizzes and exercises). Further, the AGS claim must carry
  a line item and the score scope, the platform must still be active and in the release's
  tenant, and the line item on the platform's own origin (issuer or `base_url`). Results
  after a TrueNorth-side launch of the same AU also go to that cell: it is the Student's
  grade for the AU.
- **Network.** The token and score requests go through `app/net_guard.py` (vetted and pinned
  address, no redirects, answer size capped), with private addresses only to a TrueNorth farm
  node (`app/moodle_farm`) or where `INTEGRATION_ALLOW_PRIVATE_URLS` is on (as for the
  platform probe); loopback never. The
  access token is cached per platform until a minute before it expires, and dropped on a
  401.
- **Not sent:** cmi5-allowed statements (no cmi5 category; they may carry no result,
  TN-RESULT), anything the rules refused (a forged pass), waivers (`waive` is a records
  holder's act, not a result).
- **Idempotent.** The row holds the latest result; it is sent again only when it changes, with
  the timestamp of when it was recorded, so a retry is the same Score.
- **Retries.** Network failures, 408, 425, 429 and 5xx are retried with backoff (30 s,
  doubling, at most an hour) up to `CMI5_AGS_MAX_ATTEMPTS`; a retry loop runs every
  `CMI5_AGS_RETRY_SECONDS`. Anything else (400, 401, 403, 404, 409, for example a Score the
  platform considers stale; a refused address; an error nobody foresaw) is `failed`, with
  the reason in `last_error`; the next result for that cell tries again. One row's failure
  never stops the others in a batch. Two first results for one cell at once: one insert
  wins, the other updates it. Deregistering the platform deletes its rows, also one being
  sent at that moment.

Example: Student marked 4 of 5, AU reports `passed` with `score.scaled` 0.8:

```
POST <lineitem>/scores?type_id=1           (Authorization: Bearer <client_credentials token>)
Content-Type: application/vnd.ims.lis.v1.score+json
{"timestamp": "2026-10-10T12:00:00.000Z", "activityProgress": "Completed",
 "gradingProgress": "FullyGraded", "scoreGiven": 80.0, "scoreMaximum": 100, "userId": "<lms sub>"}
```

Verified against a real Moodle 5.2.3 (`tests/integration/test_moodle_cmi5_lti.py`, CI job
`moodle`): Moodle's OIDC login and signed id_token through `/lti/login` and `/lti/launch`,
the hand-off session, the AU launched and passed with it, and 80/100 in Moodle's gradebook.
The activity there is created with the custom parameter a deep-linked item sets; Moodle's
deep-linking UI round trip itself is covered by the API tests (`tests/api/test_cmi5_lti.py`),
not driven through Moodle.

To repeat the plugin evaluation: build `infra/platform/moodle`, then an image `FROM` it that
copies each plugin (at the commits above) into `/var/www/html/public/mod/<name>` from a
`/docker-entrypoint-init.d/` hook running before `02-configure-moodle.sh`, start it with
`compose.moodle-test.yml`'s settings, and read
`php admin/cli/cfg.php --component=mod_cmi5 --name=version`.

## LRS configuration (2e)

- **lrsql on PostgreSQL** (`compose.prod.yml` `lrs`), with xAPI 1.0.3 enabled (the default,
  alongside 2.0.0): cmi5 and TrueNorth send `X-Experience-API-Version: 1.0.3`.
- **Two credentials, both held by the API only.** `LRS_AUTH` (base64 `key:secret` of the
  installer-generated `lrs_api_key`/`lrs_api_secret`) for what TrueNorth writes, and
  `CMI5_LRS_AUTH` (scoped, above) for what AUs write through it. No LRS credential reaches a
  browser: AUs get the per-session cmi5 token, valid only through TrueNorth's checking endpoint.
  Read-only dashboard credentials are issued in the lrsql admin UI, never these.
- **No CORS** needed for TrueNorth's own launches: the AU and its endpoint share the origin.
- **State documents need concurrency headers** on lrsql (a PUT over an existing document
  without `If-Match` is 409, checked against v0.9.9): TrueNorth and its AU send `If-Match` /
  `If-None-Match` on every write.
- **Retention**: the LRS holds personal information (performance, failures, timing); a privacy
  impact assessment and a retention schedule apply, set in the LRS, not by convention.

| Setting | Default | |
|---|---|---|
| `LMS_BACKEND` | `xapi_lrs` | `null` turns cmi5 launching off (503, said so). |
| `CMI5_WEB_BASE_URL` | the platform URL (`DOMAIN`, else `LTI_WEB_BASE_URL`) | Where browsers reach the SPA (AU URLs, returnURL). |
| `CMI5_API_BASE_URL` | `<web>/api` | The fetch URL and the AU's endpoint. With `TN_ENV=production` the API refuses to start if either resolves to localhost or to nothing. |
| `CMI5_SESSION_HOURS` | `12` | A session token's lifetime. |
| `CMI5_FETCH_HOURS` | `0.25` | How long after launch the fetch URL works. |
| `CMI5_LRS_AUTH` | none (cmi5 off) | The AU traffic's LRS credential; must differ from `LRS_AUTH`. |
| `CMI5_GRADE_ATTEMPTS` | `3` | Quiz marks per Student per AU per 24 hours. |
| `CMI5_AGS_RETRY_SECONDS` | `60` | How often results an LMS gradebook did not take are resent (`0`: only right after the statement). |
| `CMI5_AGS_MAX_ATTEMPTS` | `10` | Sends of one result before it is `failed`. |
| `TN_SECRETS_KEY` | (required in production) | Also keys TrueNorth's cmi5 statement ids. |

## Conformance

CI job **`cmi5`** (`.github/workflows/ci.yml`, blocking) runs two things against a fresh
stack: the itest stack with web, Keycloak and the LRS (`ITEST_WEB=1 ITEST_LRS=1
scripts/itest.sh up`), and ADL's CATAPULT (`infra/platform/docker/compose.catapult.yml`:
the Content Test Suite, the cmi5 player as its LMS, MySQL and an lrsql, built from
CATAPULT's source at commit `31a83baf0e4dad34233cc34d76818199fd60e83b`; upstream's newer
`806c0baa` does not build, its player lockfile no longer matching its manifest).

1. **The AU runtime, judged by CATAPULT's CTS** (`tests/e2e/cmi5/catapult-cts.spec.ts`,
   `playwright.cmi5.config.ts`). The CTS imports the `cmi5.xml` TrueNorth serves for C105 and
   launches AUs into Chromium; the SPA's runtime plays them for real (pages, quiz, Exit) while
   the CTS checks every request against the cmi5 requirements. It must report: AU 0 (passed)
   **conformant**, AU 1 (failed) attempted with nothing violated, AU 2 in Browse mode nothing
   violated, and no violated requirement in any session. A negative control sends a
   cmi5-defined statement before `initialized` through the same CTS and requires it to be
   caught (`9.3.0.0-4`), so a green run cannot be a vacuous one.
2. **TrueNorth's LMS side, against a real lrsql** (`tests/integration/test_cmi5_lrs.py`):
   the statements are read back from the LRS with its own credential: `LMS.LaunchData`;
   `launched` first, with the cmi5 category and the launchmode, launchurl, moveon,
   masteryscore and launchparameters extensions; a once-only fetch; `satisfied` for the block
   with a runtime id, `…/activitytype/block` and the publisher id in grouping; a refused
   statement absent from the LRS; `abandoned` on relaunch; and the legacy re-issue round trip
   (idempotent copies, voiding).

CATAPULT's **LMS** test suite (LTS) does not apply as it stands: it imports its own test
packages (ZIPs, 1000-AU structures, invalid packages to reject) into the LMS under test, and
TrueNorth launches only its own releases. The LMS-side requirements those packages exercise
for a launched AU are the ones in step 2 and in `tests/api/test_cmi5_lms.py`, which asserts
each refusal by requirement id.

Locally (Docker; on Apple silicon the amd64 images run emulated):

```bash
ITEST_WEB=1 ITEST_LRS=1 bash scripts/itest.sh up
docker compose -p tn-catapult -f infra/platform/docker/compose.catapult.yml up -d --build --wait
API_BASE_URL=http://127.0.0.1:18081 CMI5_REQUIRE_LRS=1 .venv/bin/python -m pytest tests/integration/test_cmi5_lrs.py -m integration
cd tests/e2e && npm ci && npx playwright test -c playwright.cmi5.config.ts
docker compose -p tn-catapult -f infra/platform/docker/compose.catapult.yml down -v; bash scripts/itest.sh down
```

## Limits (2026-10-09)

- TrueNorth launches only its own accepted releases; it does not import arbitrary cmi5
  packages (ZIP, 1000+ AUs), so it is not a general-purpose cmi5 LMS and CATAPULT's LMS test
  suite (which imports its own packages) does not apply to it as is.
- AUs hosted elsewhere (an absolute URL in a release's structure) are not served.
- Images and media referenced by module pages are not served by the AU runtime yet.
- Sessions left open are abandoned at the Student's next launch or by `abandon`; there is no
  sweep on expiry.
- Range modules (KB §17) are not AUs yet: lab sessions keep their own launch (Moodle LTI).
