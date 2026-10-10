# xAPI statements: identity, IRIs, registration, language, results

What every statement TrueNorth's API emits looks like, why, how to configure it, and what
happened to the statements sent before this scheme (2026-10-09). The builder is
`control-plane/api/app/xapi.py`; the database lookups that place a statement (its
registration and language) are `app/xapi_context.py`. cmi5 sessions, where the LMS
(TrueNorth or another) supplies actor, registration and activity, are [`docs/cmi5.md`](cmi5.md).

## The statement

```json
{
  "id": "1d6e0b9e-…",
  "actor": {"objectType": "Agent",
            "account": {"homePage": "https://range.example.mil", "name": "3b8f1c2e-5d4a-…"}},
  "verb": {"id": "http://adlnet.gov/expapi/verbs/passed", "display": {"en": "passed"}},
  "object": {"objectType": "Activity",
             "id": "https://range.example.mil/xapi/activities/quiz/7f0c…",
             "definition": {"type": "https://range.example.mil/xapi/activity-types/quiz",
                            "name": {"en-CA": "Log triage"}, "description": {"en-CA": "Log triage"}}},
  "result": {"score": {"raw": 20, "min": 0, "max": 25, "scaled": 0.8},
             "success": true, "duration": "PT312.40S"},
  "context": {"registration": "c41a…", "platform": "TrueNorth Range", "language": "en-CA",
              "extensions": {"https://range.example.mil/xapi/extensions/pass_threshold": 0.7,
                             "https://range.example.mil/xapi/extensions/attempt_id": "…"}},
  "timestamp": "2026-10-09T14:03:11.208+00:00"
}
```

| Part | Rule | Why |
|---|---|---|
| `actor` | Agent, `account` IFI: `homePage` = `XAPI_ACCOUNT_HOMEPAGE`, `name` = `users.id`. No `name`, no `mbox`. | An email is personal information, changes, and is forbidden as a cmi5 actor; `mbox_sha1sum` is reversible by dictionary. `users.id` is a random UUID that never changes. Reports join names from TrueNorth. |
| `verb` | ADL IRI where one exists; otherwise `<XAPI_IRI_BASE>/verbs/<word>`. Display tagged `en`. | The display is the English verb word, not course content. |
| `object.id` | `<XAPI_IRI_BASE>/activities/<type>/<id>`: identifies the quiz, exercise, objective, never an attempt. | Attempts belong in `registration` (KB §13.2). |
| `context.registration` | Every learning statement has one (below). | Without it, attempts and enrolments cannot be told apart. |
| `context.language`, language maps | The course's locale (`course_meta.locale`, `language` or `lang`), else `XAPI_DEFAULT_LANGUAGE` (`en`). `en_CA` is normalised to `en-CA`; an unusable value falls back to the default. | |
| `result.score` | `min <= raw <= max`, `scaled = raw/max` clamped to [0, 1]; with no maximum only `raw`/`min` (nothing invented). | xAPI 1.0.3 rejects anything else. |
| `result.success` | Judged against the activity's own pass mark: a quiz's `pass_pct`, the value carried as the `pass_threshold` extension. Exercises have no pass mark of their own and use `XAPI_DEFAULT_PASS_THRESHOLD` (0.7). | It used to be a hard-coded `>= 0.7` for everything. |
| `result.duration` | ISO 8601, centiseconds: quiz attempt start to submit, exercise start to completion. | |
| `timestamp` | UTC, milliseconds. | cmi5 ordering uses it. |

### Registrations

| Statement | `context.registration` |
|---|---|
| A quiz bound to a module of a course the Student is enrolled in | `enrollments.id` (the enrolment). TrueNorth's own cmi5 launches use the same value, so a Student's course record is one registration. |
| A stand-alone quiz | `quiz_attempts.id` |
| Range work: exercise started, paused, completed; objectives passed; detections credited | `exercises.id`: one registration per run, shared by everyone in it (KB §13.3, team events). |
| Sign-in/sign-out (event bus) | none: not learning activity. |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `XAPI_ACCOUNT_HOMEPAGE` | `https://$DOMAIN`, else `LTI_WEB_BASE_URL`, else `http://localhost:4200` | The identity authority. `compose.prod.yml` and the Helm chart set it (and `XAPI_IRI_BASE`) from the public origin; with `TN_ENV=production` the API refuses to start if either is unset (and `DOMAIN` too) or names localhost. |
| `XAPI_IRI_BASE` | `<same>/xapi` | Root of every TrueNorth IRI. |
| `XAPI_DEFAULT_LANGUAGE` | `en` | RFC 5646 tag for courses with no locale, e.g. `en-CA`. |
| `XAPI_DEFAULT_PASS_THRESHOLD` | `0.7` | Only for activities with no pass mark of their own. |

**Choose `XAPI_ACCOUNT_HOMEPAGE` and `XAPI_IRI_BASE` once, before data exists.** LRSs compare
IRIs and accounts as strings; changing either later splits every Student's history in two,
exactly as the email switch below did. On Helm, set them in `api.extraEnv`.

## The switch from email (migration `5a1e9c3d7b20`)

Before 2026-10-09 every statement named its actor `mbox: mailto:<email>` with a display
name, and its IRIs under `http://truenorthrange.local/`. What changed and what did not:

- **TrueNorth's database held no statements** (they went straight to the LRS;
  `external_activities.xapi_statement_id` is never written), so nothing in it was rewritten.
- **Migration `5a1e9c3d7b20`** (a data migration, reversible) creates
  `xapi_legacy_identities` and records, for every user that existed at the switch
  (soft-deleted ones included), the `mailto:` IFI their old statements carry. That keeps the
  old history attributable after someone's email changes. Downgrade drops the table; upgrading
  again rebuilds it from `users` (with the emails of that moment).
- New statements use the account actor from the first request after the upgrade. There is
  no dual-write period.

### Statements already in the LRS

xAPI statements are immutable (KB §5.10). **TrueNorth does not rewrite LRS history, and the
upgrade does not touch the LRS at all.** After the switch a Student's record is split: old
statements under their email, new ones under their account. An operator decides what to do
about the old ones, per installation, and records the decision:

1. **Leave them (the default).** Reports that span the switch join the two identities
   through `xapi_legacy_identities`. Nothing is sent.
2. **Re-issue them.** `python -m app.xapi_reissue --apply --authority <name>` (in the api
   container) stores, for every legacy statement TrueNorth wrote for a recorded user, a copy
   under the account actor with current IRIs. Only statements TrueNorth wrote are touched:
   the object under `http://truenorthrange.local/`, and the LRS `authority` that of
   TrueNorth's own `LRS_AUTH` credential, named with `--authority` (an old email alone proves
   nothing: anyone with an LRS credential could have used it). Run it without `--apply`
   first: it reports what it would send and lists the authorities it found. The copy keeps
   the original `timestamp`, points back at the original (`context.statement` StatementRef,
   and the `<XAPI_IRI_BASE>/extensions/reissued-from` extension), and its `id` is a UUID v5 of
   the original's, so a second run stores nothing new.
3. **Re-issue and void.** `--apply --void` also sends an ADL `voided` statement for each
   original, so default queries return only the copy. This is irreversible (voided statements
   remain, but only `voidedStatementId` reads them) and needs an LRS credential allowed to
   void. Use it only when the privacy assessment says the email must stop being served.

Either way, the email remains in the LRS's stored history. If the decision is that it must
not remain at all, that is an LRS-side purge under the retention schedule, outside xAPI.

## LRS

The adapter is `app/lms/` (`LMS_BACKEND`: `xapi_lrs`, or `null` to disable). Emission is
advisory: it never fails the request that caused it, and a failure is logged, never counted
as accepted. `xapi_lrs` also offers the xAPI resource API (statements queries, State and Agent
Profile documents) with the server's own credential; the `null` backend refuses those calls
(`LRSUnsupportedError`) rather than pretending.

The LRS (`yetanalytics/lrsql`, PostgreSQL in production) must accept xAPI **1.0.3**: cmi5
content and TrueNorth send `X-Experience-API-Version: 1.0.3`. lrsql enables 1.0.3 and 2.0.0
by default (`LRSQL_SUPPORTED_VERSIONS`). The LRS holds personal information (performance,
failures and times about identifiable people): a privacy impact assessment and a retention
schedule apply, and retention is set in the LRS configuration, not by convention (KB §13.1).

## Tests

- `tests/api/test_xapi.py`: identity, IRIs, configuration, registration, language, results.
- `tests/contracts/test_xapi_statement_conformance.py`: every statement shape against the
  xAPI 1.0.3 schema subset (`docs/interfaces/xapi-statement.schema.json`), including the real
  exercise-complete route.
- `tests/api/test_xapi_quiz_statements.py`: the real quiz-submit route (enrolment vs attempt
  registration, course locale, the quiz's own pass mark).
- `tests/api/test_xapi_reissue.py`: re-issue and voiding rules, the LRS resource adapter.
- `tests/api/test_migration_populated_upgrade.py` (PostgreSQL): the backfill on a populated
  database, and the downgrade.
