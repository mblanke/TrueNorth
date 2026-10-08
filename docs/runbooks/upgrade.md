# Runbook — upgrade (and roll back)

The installer (`install/`) owns deployment. An upgrade is the same `site.yml` (or
`20-fetch-app` + `30-config` + `50-stack-up`) run with the new release; the installer
detects that the running database is behind the new migrations and handles the order.
Variables: `install/inventory/group_vars/all/main.yml`.

Below, `dc` means:

```bash
dc() { docker compose -f /srv/truenorth/app/infra/platform/docker/compose.prod.yml \
                      --env-file /srv/truenorth/config/.env.production "$@"; }
```

## Which code is deployed

- **Release mode** (`tn_image_source: release`): the release's signed
  `release-manifest.json` names the images by digest and the commit (`git_sha`) whose
  compose file and migrations match them. Use the installer from that same commit.
- **Build mode** (labs): `tn_app_git_version` empty (the default) deploys the commit the
  installer itself is checked out at, so the installer and the app never drift apart. (It
  used to be a pinned SHA in the inventory, which was 336 commits stale by v1.0.0.)
- Either way the app's `compose.prod.yml` must declare the installer's compatibility level
  (`x-truenorth-installer-compat` = `install/COMPAT`); `20-fetch-app` stops on a mismatch.

## Before (T-1 day)

1. Read the release's merged PRs for: new migrations (`control-plane/api/alembic/versions/`),
   new required env vars (`.env.production.example` diff), changes to `compose.prod.yml`.
2. **Rehearse the code rollback** for this pair of releases. On any machine with a throwaway
   PostgreSQL 16:

   ```bash
   TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/postgres \
     .venv/bin/python scripts/rehearse_rollback.py --old <current release ref> --new <new ref>
   ```

   It migrates a scratch DB to the new head, populates it with the new code, runs the old code
   on it (the rollback), then the new code again, and exits 1 if anything was lost. A failure
   means **rollback = restore from backup** (B below), not "redeploy old code".
3. Check the backup escrow works (`docs/runbooks/backup-restore.md`): the pre-upgrade
   backup escrows `TN_SECRETS_KEY` and every persisted secret.
4. Announce the window ([on-call.md](on-call.md#communication)).

## Upgrade (T-0)

On the control node, in `install/` checked out at the new release's commit:

```bash
ansible-playbook site.yml -K --ask-vault-pass -e tn_release_version=<vX.Y.Z> \
  -e tn_smoke_username=<upn> -e tn_smoke_password=<password>
```

What `50-stack-up` does when the running database is behind the release
(`roles/tn_compose`, then `roles/tn_migrate`):

1. **Refuses** a database whose revision the release's migrations do not contain (it was
   migrated by a newer release), before stopping anything.
2. **Backs up** (`scripts/backup/backup.sh` into `/srv/truenorth/backups/pre-upgrade/`,
   kept out of the daily/weekly/monthly rotation). This is the rollback point; note its
   name from the output. Skip only deliberately: `-e tn_pre_upgrade_backup=false`.
3. **Stops the old code**: api, web, nginx, the three workers, beat and flower, so no old
   code ever runs against the new schema. In-flight Celery work follows
   `tn_upgrade_inflight`:
   - `drain` (default): `docker compose stop` honours each worker's `stop_grace_period`;
     `worker-provision` may take **up to an hour** to finish a range build. The play reports
     how many tasks were running.
   - `abandon` (`-e tn_upgrade_inflight=abandon`): 60 s, then running tasks are killed.
     Interrupted ranges must be torn down and rebuilt; Redis redelivers their tasks after the
     broker visibility timeout (3600 s), and the worker's fencing skips stale ones. Purge the
     queues first if you do not want that.
4. `alembic upgrade head` in a one-shot container of the **new** api image, straight against
   `postgres:5432` (not pgbouncer), with `DATABASE_URL` passed through the environment, not
   the command line.
5. Starts everything as the new release and waits for every healthcheck; `95-smoke-test`
   then proves health, readiness (database, Redis, OpenSearch) and the sign-in claim contract.

Verify by hand too: log in as an instructor, open a course, start and stop a small range.

## Roll back

The installer refuses a release older than the one recorded in
`/srv/truenorth/state/app.json` (or a commit that is an ancestor of the deployed one), and a
database newer than the code. Rolling back is therefore always explicit, with
`-e tn_allow_downgrade=true`. Two kinds:

**A. Code rollback, schema kept** (valid only when the rehearsal in "Before" passed —
migrations are additive):

```bash
# control node, install/ checked out at the PREVIOUS release's commit
ansible-playbook site.yml -K --ask-vault-pass -e tn_release_version=<previous> \
  -e tn_allow_downgrade=true
```

With the database ahead of the code, `tn_migrate` skips `alembic upgrade` (the old code's
alembic cannot place the newer head) and starts the old release. Do **not** run
`alembic downgrade` for a code rollback.

**B. Data rollback** (a migration damaged data, or the rehearsal failed): restore the
pre-upgrade backup, then install the previous release.

```bash
# platform host
cd /srv/truenorth/app && set -a; . /srv/truenorth/config/backup.env; set +a
scripts/backup/restore.sh /srv/truenorth/backups/pre-upgrade/<the backup>
# control node, install/ at the previous release's commit
ansible-playbook site.yml -K --ask-vault-pass -e tn_release_version=<previous> -e tn_allow_downgrade=true
```

Everything written since the backup is lost — say so in the incident record.

Schema downgrade (`alembic downgrade <rev>`) exists and is tested
(`tests/api/test_migration_populated_upgrade.py`), but use it only on advice from whoever
wrote the migration: e.g. `23df1b265fd2`'s downgrade **unseals** stored credentials back to
plain text.

## After

- Keep the pre-upgrade backup until the next release is stable. It lives in
  `backups/pre-upgrade/`, which `cron-backup.sh` does not rotate; only the overall size cap
  (`tn_backup_max_total_gb`) can remove it, oldest first, never the newest backup.
- Update the release notes with the migration head and anything an operator had to do.
