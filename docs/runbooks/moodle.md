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
`net_guard`. Publishing does not depend on it.

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
once every module is complete). The API pulls every active Moodle every 10 minutes
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
activities and TrueNorth-created accounts are reported, named by their TrueNorth id only.
TrueNorth then records a row only for a user of the platform's tenant, enrolled in
TrueNorth on a course published to that Moodle; Moodle never creates or reopens an
enrolment. Recording is idempotent (`reset: true` re-reads from the start safely).

| Symptom | Look at |
|---|---|
| `last_error`: "not signed by its registered key" | The platform's `lti_jwks_url` must be the node's `/mod/lti/certs.php`, reached through `base_url`. |
| `skipped.not_enrolled` | The Student opened the Moodle course without a TrueNorth enrolment (staff previewing, or an enrolment removed). |
| `skipped.course_not_published_here` | Grades in a Moodle course TrueNorth did not publish to this platform: ignored by design. |

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
