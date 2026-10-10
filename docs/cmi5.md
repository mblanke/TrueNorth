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
| `GET /cmi5/releases/{id}/aus/{n}/content` | signed in, same tenant | Pages and quiz questions for the AU runtime. Never the answers. |
| `POST /cmi5/releases/{id}/aus/{n}/grade` | signed in, same tenant | Marks the AU's quiz on the server: `{correct, total, scaled}`. |
| `POST /cmi5/releases/{id}/aus/{n}/launch` | an enrolled Student | TrueNorth's launch: returns the AU URL with the cmi5 parameters. |
| `POST /cmi5/fetch/{secret}` | the secret | The fetch URL: the session's auth token, once. |
| `GET/POST/PUT/DELETE /cmi5/lrs/{resource}` | the session token (Basic) | The AU's xAPI endpoint. |
| `POST /cmi5/sessions/{id}/abandon` | the Student, or `learning_record:write` | Ends an open session (`abandoned`). |
| `POST /cmi5/registrations/{id}/aus/{n}/waive` | `learning_record:write` | `waived` with a reason, then moveOn. |

Candidate (not yet accepted) releases are visible to course authors only. Another tenant's
release is 404.

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
   {"id": "<uuid5(session:launched)>", "actor": {"objectType": "Agent",
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
   (`CMI5_FETCH_HOURS`), is stored hashed and is never logged.
3. **The AU** reads `LMS.LaunchData` and `cmi5LearnerPreferences`, then sends `initialized`,
   cmi5-allowed `progressed` per page, `completed` after the last page, `passed` or `failed`
   from the server-marked quiz (with the `masteryscore` extension and `score.scaled`/`raw`/
   `min`/`max`), and `terminated` on Exit (or, best effort with `keepalive`, when the window
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
| `LMS.LaunchData` read-only; State and Profile only for this actor and activity; learner preferences read-only | 10.2.1.0-5, 10.1.0.0-3 |
| no voiding | 6.3.0.0-1 |

The AU's token cannot read statements back (least privilege; cmi5 does not need it) and is
dead once the session is terminated, abandoned, or 12 hours old (`CMI5_SESSION_HOURS`).

### Delivery guarantees

- TrueNorth's own statements (launched, abandoned, waived, satisfied) are written with
  `PUT /statements?statementId=` and ids derived from what they record (UUID v5), so a retry
  never duplicates; a conflict on that id means it is already stored, and stands.
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

- **Sign-in.** The AU reads the module's pages from TrueNorth, so the Student signs in to
  TrueNorth (single sign-on with the LMS where both use the same Keycloak, as the Moodle
  farm does). The query parameters survive the sign-in round trip.
- **Reaching the LMS's endpoint.** The production CSP allows the SPA to connect to its own
  origin and to any https port on its own host (the farm Moodle). An LMS on another host
  needs its origin added to `connect-src` in `infra/platform/nginx/snippets/security-headers.conf`,
  and its LRS must allow TrueNorth's origin in CORS.

## LRS configuration (2e)

- **lrsql on PostgreSQL** (`compose.prod.yml` `lrs`), with xAPI 1.0.3 enabled (the default,
  alongside 2.0.0): cmi5 and TrueNorth send `X-Experience-API-Version: 1.0.3`.
- **One server credential**, `LRS_AUTH` (base64 `key:secret` of the installer-generated
  `lrs_api_key`/`lrs_api_secret`), held by the API only. No LRS credential reaches a browser:
  AUs get the per-session cmi5 token, valid only through TrueNorth's checking endpoint. Read-only
  dashboard credentials are issued in the lrsql admin UI, never this one.
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
| `CMI5_API_BASE_URL` | `<web>/api` | The fetch URL and the AU's endpoint. |
| `CMI5_SESSION_HOURS` | `12` | A session token's lifetime. |
| `CMI5_FETCH_HOURS` | `0.25` | How long after launch the fetch URL works. |

## Limits (2026-10-09)

- TrueNorth launches only its own accepted releases; it does not import arbitrary cmi5
  packages (ZIP, 1000+ AUs), so it is not a general-purpose cmi5 LMS and CATAPULT's LMS test
  suite (which imports its own packages) does not apply to it as is.
- AUs hosted elsewhere (an absolute URL in a release's structure) are not served.
- Images and media referenced by module pages are not served by the AU runtime yet.
- Sessions left open are abandoned at the Student's next launch or by `abandon`; there is no
  sweep on expiry.
- Range modules (KB §17) are not AUs yet: lab sessions keep their own launch (Moodle LTI).
