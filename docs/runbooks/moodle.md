# Moodle: install it, publish a course into it, take it as a Student

TrueNorth's Moodle is optional. The installer runs it when `tn_moodle_enabled: true`
(`install/playbooks/85-moodle.yml`, `install/roles/tn_moodle`). Design and background:
`docs/adr/0004-moodle-course-publication.md`, `docs/moodle-primer.md`.

## What gets installed

| Piece | Where |
|---|---|
| Moodle 5.2.3 with `local_truenorth` (image `ghcr.io/mblanke/truenorth-moodle`, by digest from the release's `release-manifest.json`) | compose project `truenorth-moodle-<node>` (`infra/platform/docker/compose.moodle-prod.yml`), node `tn_moodle_node` (`default`) |
| Its database | `moodle` (role `moodle`) in the platform's PostgreSQL |
| Its files | `/srv/truenorth/moodle/<node>/moodledata` |
| Its address | `https://<tn_domain_fqdn>:<tn_moodle_port>` (8443): `moodle-edge`, an nginx with the platform's certificate. Its own origin on purpose: HTML authored in Moodle must not run where TrueNorth's sign-in lives. |
| Its credentials | `/srv/truenorth/config/secrets/moodle_db_password`, `moodle_admin_password` (escrowed with every backup like the others); `/srv/truenorth/config/moodle-<node>.env` (0600) |
| Its registration in TrueNorth | platform `moodle-<node>` in tenant `tn_tenant_slug`: LTI 1.3 URLs on the public address, server-side address `http://moodle-<node>:8080` on `tn-backend` |

How the two trust each other: TrueNorth's LTI tool key lives in TrueNorth's database. The
node fetches its public half from `http://api:8080/lti/public-key.pem` on every start and
trusts it for LTI launches, sign-in tickets and course sync; it accepts tickets for its own
tenant only (`TN_TENANT_ID`). TrueNorth stores no Moodle token, and Moodle calls nothing.
The other way, TrueNorth trusts the node's LTI site key (its `/mod/lti/certs.php`): for
LTI launches, and for the results the node signs when TrueNorth pulls them (section 4).

## 1. Install

The operator provides: `tn_moodle_enabled: true` for the host, port `tn_moodle_port`
(8443) reachable wherever 443 is, and a release that publishes the Moodle image (the first
release after this change; older ones stop with "has no moodle image by digest"). Nothing
else: no DNS record, no certificate name, no password. Optional: `vault_moodle_db_password`
/ `vault_moodle_admin_password` (32+ characters of `A-Z a-z 0-9 . _ -`), and
`tn_moodle_smtp_host` if Moodle may send mail (the default sends nowhere).

```bash
# in the host's inventory entry: tn_moodle_enabled: true
ansible-playbook -i inventory/staging.yml site.yml -K --ask-vault-pass -e tn_release_version=<tag>
# or, on an installed platform, just this stage (and 30-config, which adds Moodle to the
# nightly backup):
ansible-playbook -i inventory/staging.yml playbooks/30-config.yml playbooks/85-moodle.yml -K --ask-vault-pass -e tn_release_version=<tag>
```

A first install creates Moodle's tables: a few minutes. The stage ends with three checks:
the login page answers through the edge, Moodle's sign-in page hands people to TrueNorth,
and TrueNorth's own publishing backend gets a signed answer from `local_truenorth`
(`python -m app.moodle_backends.install_cli check`). A second run changes nothing.

The platform's **Integrations → Test** button reports this Moodle as refused unless
`INTEGRATION_ALLOW_PRIVATE_URLS` is on: it probes the internal address through
`net_guard`. Publishing does not depend on it, and neither do grades sent back to this
Moodle (cmi5 over LTI): `install_cli register` marks the node a TrueNorth farm node
(`app/moodle_farm`), which may reach its private address and binds Students' farm accounts
by their locked TrueNorth id (docs/moodle-integration.md, "Farm nodes"). A node registered
before 2026-10-10 is marked on the installer's next run; one added with
`scripts/moodle-farm.sh` is marked by the script (`install_cli manage`).

Break-glass administrator: `https://<fqdn>:8443/login/index.php?loginredirect=0`, user
`admin`, password in `/srv/truenorth/config/secrets/moodle_admin_password`. Nobody else
logs in to Moodle directly.

With a self-signed certificate (`tn_tls_mode: selfsigned`) browsers ask to trust it for
`:8443` separately from the platform.

## 2. Publish a course release

As an instructor or admin (`course:release`):

1. **Authoring → Courses → Upload release**: the `.tar.gz` built from an accepted ARC² run
   (`python -m arc2.release build`).
2. **Accept** it. Its content becomes the course's.
3. **Publish**, choose `Moodle (default)`. The chip goes `Moodle: requested` → `staging`
   → `verifying` → `activating` → `published`. A failure shows its reason on the chip, with **Retry**.

The same through the API: `POST /api/course-releases/{release_id}/publications
{"platform_id": "<id of moodle-default>"}` (`GET /api/integrations/platforms` lists it).

Publishing stages the whole course hidden, verifies it from Moodle's own description, then
converges the live course (its idnumber is the TrueNorth course id). Students keep the
previous release until then.

## 3. Take it as a Student

1. The Student needs an account (no AD: a local Keycloak user in `TN-Students`, then the
   registration form and an admin's approval under **Users → Approvals**) and an
   enrolment in the TrueNorth course: approving them onto the course's qualification
   enrols them, or `POST /api/courses/{course_id}/enroll {"user_id": "<their id>"}`
   (themselves, or an admin for anyone in the tenant).
2. Signed in to TrueNorth, the Student opens the course (**Learning → Courses**) and
   clicks **Open in Moodle**. A new tab lands in the Moodle course, signed in: no Moodle
   password. The ticket lives 60 s and is single-use.
3. Pages, quizzes (native Moodle quizzes: Moodle keeps the attempt, grade and
   completion) and range labs (LTI links back into TrueNorth) are in the course.

Staff open the same course as Moodle teachers. **Open in Moodle** appears only once the
course is published to the tenant's Moodle.

## 4. Results come back to TrueNorth

Completions and quiz grades a Student earns in Moodle are pulled into their TrueNorth
record: module progress (score, completed), a quiz attempt mirroring Moodle's grade for
the module's quiz, and the enrolment (in progress, then completed with its letter grade
once every module is complete). A module completed in Moodle with nothing graded (say,
readings) is recorded as not scored (`max_score` 0): a pass that neither helps nor drags
the course grade. Moodle course completions are not used. A mirrored attempt does not use
up the quiz's TrueNorth `max_attempts`. The API pulls every active Moodle every 10 minutes
(`MOODLE_RESULTS_PULL_SECONDS`, `0` turns it off) and an admin can pull now:

```http
POST /api/integrations/platforms/{platform_id}/moodle-results/pull   {"reset": false}
→ 200 {"platform_id": "…", "cursor": "1760001234.2.88", "pages": 1, "rows": 14,
       "applied": 12, "unchanged": 0, "skipped": {"not_enrolled": 2}, "more": false}
GET  /api/integrations/platforms/{platform_id}/moodle-results
→ 200 {"cursor": "…", "last_run_at": "…", "last_success_at": "…", "last_error": "",
       "rows_seen": 14, "rows_applied": 12, "running": false}
```

How it is trusted: TrueNorth asks with a sync ticket (as for publishing); `local_truenorth`
answers with a JWT signed by the Moodle's **LTI site key**, which TrueNorth verifies against
the platform's registered `lti_jwks_url`, issuer `lti_issuer`, the platform's tenant and
the jti of the ticket it answers. An unsigned, re-signed or replayed answer is refused
(502, kept in `last_error`) and nothing is recorded. Only TrueNorth's courses, `tn:`
activities and TrueNorth-created accounts are reported, named by their TrueNorth id only;
an account counts as TrueNorth-created only when its username is `tn-<that id>`, and the
plugin locks the ID number field (`auth_manual` `field_lock_idnumber`) so a Student
cannot change it. Every fact is recorded per course.
TrueNorth then records a row only for a user of the platform's tenant, enrolled in
TrueNorth on a course published to that Moodle; Moodle never creates or reopens an
enrolment. Recording is idempotent (`reset: true` re-reads from the start safely).

| Symptom | Look at |
|---|---|
| `last_error`: "not signed by its registered key" | The platform's `lti_jwks_url` must be the node's `/mod/lti/certs.php`, reached through `base_url`. |
| `skipped.not_enrolled` | The Student opened the Moodle course without a TrueNorth enrolment (staff previewing, or an enrolment removed). |
| `skipped.course_not_published_here` | Grades in a Moodle course TrueNorth did not publish to this platform: ignored by design. |
| `skipped.not_this_tenants_user` | A row names a TrueNorth id that is not a user of the platform's tenant. Results match on ids only, never on email, so a same-address person in another tenant gets nothing. |

**How fresh, and where it shows.** Each fact keeps Moodle's own time (`source_time`), the
release that made the activity and, for a quiz grade, Moodle's attempt id. The Student sees
their own on the course page (**Learning → Courses → the course**), under "Open in Moodle":
"Module 1 quiz: 80 / 100, in Moodle <time>", "Module 1 page: complete", and the sync line.
The same through the API, for the Student or (with `learning_record:read`) anyone in the
tenant:

```http
GET /api/integrations/moodle/results?course_id=<id>[&user_id=<id>]
→ 200 {"enrollment_id": "…", "enrollment_status": "in_progress",
       "sync": [{"platform_name": "Moodle (default)", "state": "current", "stale": false,
                 "last_success_at": "…", "last_error": "", "sync_interval_seconds": 600}],
       "results": [{"kind": "quiz_grade", "activity": "tn:mod_001:quiz:…", "grade": 80.0,
                    "grade_max": 100.0, "source_time": "…", "source_attempt_id": 42,
                    "release_id": "…", "recorded_at": "…"}, …]}
```

| Sync state | Means | The course page says |
|---|---|---|
| `current` | the last pull succeeded within two intervals | "Moodle results synced <time>" |
| `stale` | no successful pull for more than two intervals (`2 × MOODLE_RESULTS_PULL_SECONDS`) | "may be out of date: last synced <time>" |
| `failing` | the latest pull failed (Moodle down, or its answer refused); `last_error` (staff only) says why | "may be out of date: the last sync failed" |
| `never` | no pull has succeeded yet | "not synced yet" |

A result Moodle holds while a pull fails is not lost: the cursor stays where it was, and the
next successful pull records it once (the ledger is idempotent; `reset` re-reads safely).

## 5. Student loop on staging (about 5 minutes)

A person's click-through of M1 on the installed host, in a real browser. The automated
evidence is the CI `moodle` lane (the same loop in Chromium) and the acceptance check
`lms / Moodle loop` (the same loop over HTTP, without a browser); this is the part only a
person can do: sign in through Keycloak with a real Student account and look.

**Before** (once): a Student test account you can sign in as, enrolled on a course that is
published to Moodle (its release chip reads `Moodle: published`); a second Student account
not enrolled on it; a shell on the host. Use a private browser window per account. Note the
time; you will record it.

1. **TrueNorth (30 s).** Sign in at `https://<fqdn>` as the enrolled Student. **Learning →
   Courses →** the course. Expect **Open in Moodle** and a line "Moodle results synced …" (or
   "not synced yet").
2. **Into Moodle (30 s).** Click **Open in Moodle**. A new tab opens the Moodle course on
   `https://<fqdn>:8443`, signed in as the Student (their name at top right) with no Moodle
   login page. The address bar never shows the ticket.
3. **A page (15 s).** Open the first page of module 1. Back on the course page its completion
   tick is filled.
4. **The quiz, leave and resume (2 min).** Open the module 1 quiz → **Attempt quiz** → answer
   question 1 → **Next page**. Go back to the course page (leave). Open the quiz again →
   **Continue your attempt**: you are on question 2, question 1 still answered. Answer the
   rest → **Finish attempt …** → **Submit all and finish** → confirm. Write down the grade
   the review page shows.
5. **Pull (30 s).** Wait 10 seconds (Moodle's settle window), then on the host:

   ```bash
   cd /srv/truenorth/app/infra/platform/docker
   docker compose -p docker -f compose.prod.yml --env-file /srv/truenorth/config/.env.production \
     exec -T api python -c 'from app.moodle_results.runner import pull_all; print(pull_all(), "Moodle(s) pulled")'
   ```

   (or wait for the scheduled pull, every `MOODLE_RESULTS_PULL_SECONDS`, 10 minutes by default).
6. **Back in TrueNorth (30 s).** Reload the course page. Expect "Moodle results synced
   <a minute ago>", "Module 1 quiz: <the grade from step 4, out of 100> … in Moodle <the time
   you submitted>" and "Module 1 page: complete".
7. **Refusals (1 min).**
   - In the other private window, sign in as the Student who is **not** enrolled, open the
     same course and click **Open in Moodle**: "You are not enrolled in this course", and no
     Moodle tab stays open.
   - In a third private window (no session anywhere), open `https://<fqdn>:8443/`: Moodle has
     no login of its own and sends you to TrueNorth.
   - Reused, expired and other-tenant tickets cannot be made by hand without developer tools;
     the CI `moodle` lane refuses each in a browser (`test_moodle_student_journey.py`).

**Pass** when every expectation above holds. **Record** in the release notes (staging
record): date and time, the two accounts (usernames, never passwords), the course, the
grade in Moodle and in TrueNorth with both times, the sync line, and screenshots of steps 2,
4 (review page), 6 and the not-enrolled message. A failure at step 6 with "may be out of date: the last sync
failed": read `last_error` (section 4) before anything else.

## Backups, restore, upgrades

- The nightly backup holds the `moodle` database (every database is dumped) and
  `moodle/moodledata.tar` (without caches), when `backup.env` names the node
  (`MOODLE_COMPOSE_FILE`, rendered by `30-config` with Moodle enabled).
- `scripts/backup/restore.sh` stops the node with the platform, restores its database with
  the others, replaces moodledata with the backup's, and starts the node after the
  platform. `--skip-moodle` leaves the files alone.
- An upgrade that brings a new Moodle image recreates the node; Moodle upgrades its own
  database on start. The pre-upgrade backup holds both halves.

## Removing it

`tn_moodle_enabled: false` stops managing Moodle; it does not remove it, and backups then
skip moodledata. To remove it: mark the platform inactive (Integrations), then
`docker compose -f compose.moodle-prod.yml --env-file /srv/truenorth/config/moodle-default.env down`;
the database and `/srv/truenorth/moodle/` stay until you drop and delete them.

## More than one tenant

Open-source Moodle has no tenants, so the model is one node per TrueNorth tenant
(`docs/moodle-primer.md`, "The farm"). The pieces already take a node name: project
`truenorth-moodle-<node>`, alias `moodle-<node>`, data `/srv/truenorth/moodle/<node>/`,
env `moodle-<node>.env`, platform `moodle-<node>`, and `install_cli` takes the tenant slug.
What a farm adds: a list of `{node, tenant_slug, port}` looped over by the role, one
database per node (`moodle_<node>`), one edge port each, and backup.env naming every node.
Not built yet.

## Troubleshooting

| Symptom | Look at |
|---|---|
| The stage times out bringing the node up | `docker logs truenorth-moodle-default-moodle-1`: the first install, a database error, or `[truenorth] ERROR: no TrueNorth public key` (the api was not reachable on `tn-backend`). |
| `install_cli check` says `ticket refused` | The node's tenant (`TN_TENANT_ID` in its env file) is not the platform's, or its key is stale: restart the node after a tool-key rotation. |
| Moodle redirects to another address | Its `SITE_URL` (`tn_moodle_site_url`) must be exactly the address browsers use, port included. |
| "Open in Moodle" does nothing | The SPA's CSP allows forms to `https://<this host>:<any port>`; a Moodle on another host needs that host added to `form-action` (`infra/platform/nginx/snippets/security-headers.conf`). |
