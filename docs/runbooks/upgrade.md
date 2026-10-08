# Runbook — upgrade (and roll back)

The installer (`install/`) owns deployment: `20-fetch-app.yml` checks out
`tn_app_git_version` into `/srv/truenorth/app`; `50-stack-up.yml` runs `tn_config`,
`tn_compose` (datastores) and `tn_migrate` (schema, then the full stack). Variables:
`install/inventory/group_vars/all/main.yml`.

Below, `dc` means:

```bash
dc() { docker compose -f /srv/truenorth/app/infra/platform/docker/compose.prod.yml \
                      --env-file /srv/truenorth/config/.env.production "$@"; }
```

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
   means **rollback = restore from backup**, not "redeploy old code": plan the window for it.
   Record the output beside `docs/hardening/rollback-rehearsal-*.md`.
3. Add any new env vars to `/srv/truenorth/config/.env.production` (or the installer vault).
   `TN_SECRETS_KEY` must be present once the sealed-credentials migration (`23df1b265fd2`)
   is in the release: it refuses to run without the key if any plaintext credential exists.
4. Announce the window ([on-call.md](on-call.md#communication)).

## Upgrade (T-0)

1. **Backup, and check it.**

   ```bash
   cd /srv/truenorth/app && set -a; . /srv/truenorth/config/backup.env; set +a
   scripts/backup/backup.sh && echo OK
   ```

   Exit 0 required (3 = no escrow: fix that first — an upgrade that seals credentials is
   exactly when you need the key). Note the directory name; it is the rollback point.
2. Record the current state: `git -C /srv/truenorth/app rev-parse HEAD` and
   `dc exec -T postgres psql -U <user> -d <db> -Atc 'select version_num from alembic_version'`.
3. Drain: stop accepting new exercises (announce), let running provisioning finish
   (Flower shows no active or reserved tasks).
4. Fetch and migrate (on the control node, in `install/`):

   ```bash
   ansible-playbook playbooks/20-fetch-app.yml --ask-vault-pass -e tn_app_git_version=<new ref>
   ansible-playbook playbooks/50-stack-up.yml  --ask-vault-pass
   ```

   `tn_migrate` runs `alembic upgrade head` in a one-shot `api` container straight against
   `postgres:5432` (not pgbouncer — transaction pooling and DDL do not mix), then brings up
   the whole stack and waits for every healthcheck. To run only the migration by hand:

   ```bash
   dc run --rm --no-deps --entrypoint "" \
     -e DATABASE_URL=postgresql+psycopg://<user>:<pass>@postgres:5432/<db> \
     api alembic -c alembic.ini upgrade head
   ```

5. Verify: `ansible-playbook playbooks/95-smoke-test.yml`, then `curl -fsS https://$DOMAIN/api/health/deep`,
   log in as an instructor, open a course, start and stop a small range.

## Roll back

Decide within the window. Two kinds:

**A. Code rollback, schema kept** (the normal case; valid when the rehearsal in step 2 of
"Before" passed — migrations are additive):

```bash
# control node, in install/
ansible-playbook playbooks/20-fetch-app.yml --ask-vault-pass -e tn_app_git_version=<previous ref>
# platform host: rebuild and restart on the old code WITHOUT tn_migrate — the old code's
# alembic does not know the new head and `upgrade head` would fail.
dc up -d --build
```

Do **not** run `alembic downgrade` for a code rollback. Known effects (from the rehearsal):
range actions taken while rolled back leave no operation record; operations accepted but not
yet sent are sent after the roll forward, and the worker's fencing skips stale ones.

**B. Data rollback** (a migration damaged data, or the rehearsal failed): restore the backup
taken in step 1, then deploy the previous ref as in A.

```bash
scripts/backup/restore.sh /srv/truenorth/backups/<the step-1 backup>
```

Everything written since the backup is lost — say so in the incident record.

Schema downgrade (`alembic downgrade <rev>`) exists and is tested
(`tests/api/test_migration_populated_upgrade.py`), but use it only on advice from whoever
wrote the migration: e.g. `23df1b265fd2`'s downgrade **unseals** stored credentials back to
plain text.

## After

- Keep the pre-upgrade backup out of rotation until the next release is stable
  (copy it to `/srv/truenorth/backups/pre-upgrade/`; `cron-backup.sh` does not rotate that).
- Update the release notes with the migration head and anything an operator had to do.
