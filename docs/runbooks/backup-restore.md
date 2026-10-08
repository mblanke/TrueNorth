# Runbook — backup and restore

Scripts: `scripts/backup/` (`backup.sh`, `restore.sh`, `restore-secrets.sh`,
`cron-backup.sh`, `escrow-open.sh`, `drill.sh`, shared `lib.sh`). Proof: `scripts/backup/drill.sh`, run by
`.github/workflows/backup-drill.yml` nightly and on every change to `scripts/backup/**`,
`compose.prod.yml` or `postgres-init/`.

## What a backup contains

`$BACKUP_DIR/<daily|weekly|monthly|pre-upgrade>/truenorth-backup-<UTC>/` (`pre-upgrade/` is
written by the installer before it migrates; [upgrade.md](upgrade.md))

| Path | Content |
|---|---|
| `postgres/globals.sql` | roles and their (hashed) passwords — `pg_dumpall --globals-only` |
| `postgres/<db>.dump` | `pg_dump -Fc` of **every** non-template database on the server: the app (`$POSTGRES_DB`), `keycloak`, `lrs`, `postgres`, and any added later. Discovered at run time, each checked with `pg_restore --list`. |
| `minio/<bucket>/` | every bucket, `mc mirror` (empty buckets kept as empty directories) |
| `secrets/env.enc`, `secrets/env.key.enc` | the stack's env file — which holds `TN_SECRETS_KEY` and every credential — AES-256 encrypted with a fresh data key, that key RSA-OAEP-wrapped to the escrow public key |
| `secrets/secrets.tar.enc` | `SECRETS_DIR` (the installer's `/srv/truenorth/config/secrets/`, one file per persisted secret), tarred straight into the same cipher with the same data key; never on disk in clear. `restore-secrets.sh` writes it back. |
| `manifest.json` | databases and sizes, bucket/object counts, OpenSearch and escrow status, compose project, git commit |
| `SHA256SUMS` | checksums of everything above; verified at the end of the backup and before any restore |

Not included, on purpose:

- **Redis** — Celery broker, cache, rate-limit counters. Restoring a stale queue would replay
  provisioning tasks. After a restore Redis starts empty; that is correct.
- **OpenSearch** — see [OpenSearch](#opensearch). Recorded as `not-backed-up` in the manifest
  unless a snapshot repository is configured.
- **Plain-text secrets** — the old script copied `.env.production` and `terraform.tfstate`
  into the backup in clear. It no longer does. Configuration is in git; the manifest records
  the commit.

A backup is written to `.partial-truenorth-backup-…` and renamed only when complete; a failed
run deletes its partial directory. One run at a time (`$BACKUP_DIR/.lock`).

## What the operator must set

The installer writes `{{ tn_config_dir }}/backup.env` (default `/srv/truenorth/config/backup.env`)
and a cron entry (`install/roles/tn_config/tasks/main.yml`, 02:17 nightly, log
`/srv/truenorth/logs/backup.log`, rotated by logrotate). **The install stops** unless the
inventory sets the escrow: `tn_backup_escrow_pubkey` (a public key on the control node, copied
to `/srv/truenorth/config/backup-escrow.pub`) or `tn_backup_escrow: out-of-band`.

```bash
# Written by the installer:
COMPOSE_FILE=/srv/truenorth/app/infra/platform/docker/compose.prod.yml
ENV_FILE=/srv/truenorth/config/.env.production
BACKUP_DIR=/srv/truenorth/backups
RETENTION_DAYS=30             # ignored under cron-backup.sh (it rotates by count)
SECRETS_DIR=/srv/truenorth/config/secrets
BACKUP_ESCROW_PUBKEY=/srv/truenorth/config/backup-escrow.pub   # or BACKUP_ESCROW=out-of-band
BACKUP_MIN_FREE_GB=20         # tn_backup_min_free_gb: refuse to start below this (or below the last backup's size)
BACKUP_MAX_TOTAL_GB=200       # tn_backup_max_total_gb: prune oldest (never the newest) above this
OPENSEARCH_SNAPSHOT_CACERT=config/certs/ca.pem

# Optional, add by hand
# DAILY_KEEP=7 WEEKLY_KEEP=4 MONTHLY_KEEP=12
# S3_BUCKET=s3://truenorth-backups  S3_ENDPOINT=...  S3_ENCRYPT=1  GPG_RECIPIENT=...
# BACKUP_WEBHOOK_URL=https://...    # extra alert channel; stderr + syslog always alert
# OPENSEARCH_SNAPSHOT_REPO=tn_snapshots
# COMPOSE_PROJECT_NAME=...          # only if the stack was started with -p
```

Disk: a backup does not start with less than `BACKUP_MIN_FREE_GB` free (or less than the
previous backup took), and after each one the oldest backups across `daily/`, `weekly/`,
`monthly/` and `pre-upgrade/` are pruned while all of them exceed `BACKUP_MAX_TOTAL_GB`.

Run by hand without escrow, the backup still runs, but `backup.sh` exits **3** and alerts:
`TN_SECRETS_KEY` would not be recoverable, and every sealed credential (hypervisor passwords,
API tokens, AI engine keys) would have to be re-entered after a restore onto a fresh host.

### Create the escrow key (once, on a trusted machine — not the backup host)

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out backup-escrow.key
openssl pkey -in backup-escrow.key -pubout -out backup-escrow.pub
# backup-escrow.pub -> /srv/truenorth/config/backup-escrow.pub on the host (mode 0644)
# backup-escrow.key -> offline: password manager / HSM / sealed envelope, two custodians
```

The manifest records `recipient_sha256` so you can tell which key a backup was sealed to
(`openssl pkey -pubin -in backup-escrow.pub -outform DER | openssl dgst -sha256`).

### Alerts

Every failure goes to stderr (captured in `backup.log` / cron mail) and to syslog with tag
`truenorth-backup` (`journalctl -t truenorth-backup`), plus the webhook if set. Monitor for:
non-zero exit, no new `truenorth-backup-*` directory in 26 h, or a `[ALERT]` line.
Exit codes: `1` failed (nothing kept), `3` data kept but no escrow, `4` offsite upload failed.

## Run a backup by hand

```bash
cd /srv/truenorth/app
set -a; . /srv/truenorth/config/backup.env; set +a
scripts/backup/backup.sh            # or scripts/backup/cron-backup.sh for the rotated layout
```

It uses `docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -T <service>`, i.e. the
same compose project the installer started — no container names assumed. Database
credentials come from the env file; MinIO credentials are handed to the `mc` container as
inherited environment variables, never on a command line. `mc` comes from the stack's own
pinned MinIO image (`MC_IMAGE` to override).

## Rebuilt host: secrets first, then data, then the installer

The restored databases hold the OLD passwords (`globals.sql`), and `TN_SECRETS_KEY` sealed the
stored credentials. A host rebuilt from scratch (or reinstalled, which generated FRESH
secrets) must get the backup's secrets back before anything else:

```bash
# 1. On the platform host, with the escrow private key brought in for the occasion:
cd /srv/truenorth/app
scripts/backup/restore-secrets.sh /srv/truenorth/backups/<type>/<backup> /path/to/backup-escrow.key \
  --secrets-dir /srv/truenorth/config/secrets [--force]
#    --force only if the installer already ran here and generated fresh secrets: they are
#    kept as config/secrets.replaced-<UTC>, never deleted. Then remove the private key.
# 2. Control node: re-run the installer. It finds the restored secrets in config/secrets/,
#    renders the env file from them and recreates the services with them. (Vault values must
#    equal them or be empty: 30-config refuses a differing first-start secret.)
ansible-playbook site.yml -K --ask-vault-pass        # smoke-test credentials as usual
# 3. Restore the data (below), then 95-smoke-test again.
```

`scripts/backup/drill.sh` proves exactly this path (step 8): fresh secrets, restore over them,
data restore, and the restored database passwords authenticate while the fresh ones do not.

OpenSearch is not in the backup: on a rebuilt host its security index was initialised from
whatever `internal_users.yml` held at first start. If that was before the secrets were
restored, load the restored passwords with `playbooks/rotate-secret.yml -e
tn_rotate=opensearch_admin_password` (and `opensearch_dashboards_password`), or wipe
`/srv/truenorth/opensearch` before the installer's first run.

## Restore

Pre-checks:

1. Pick the backup: `ls -1d $BACKUP_DIR/*/truenorth-backup-* | sort | tail`. Read its
   `manifest.json`.
2. On a new host, restore the secrets first ("Rebuilt host", above). To read just the env
   file: `scripts/backup/escrow-open.sh <backup> backup-escrow.key <out>` (run where the
   private key is).
3. The stack must be up (`postgres` and `minio` at least) — e.g. after the installer ran with
   the restored secrets.

Run:

```bash
cd /srv/truenorth/app
set -a; . /srv/truenorth/config/backup.env; set +a
scripts/backup/restore.sh $BACKUP_DIR/daily/truenorth-backup-20261008T021700Z
#   [--force]          skip the "type the project name" prompt
#   [--no-restart]     leave the app services stopped afterwards
#   [--skip-postgres | --skip-minio]
```

What it does: verifies `SHA256SUMS` → stops every running service except `postgres` and
`minio` → applies `globals.sql` (existing roles are kept; their attributes and password
hashes are reset to the backup's) → for each `postgres/<db>.dump`:
`DROP DATABASE … WITH (FORCE)` then `pg_restore --create --exit-on-error` → for each bucket:
`mc mb --ignore-existing` then `mc mirror --overwrite` (objects created after the backup are
left in place) → starts the services it stopped.

After:

```bash
# Row counts of the tables that matter
docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -T postgres \
  psql -U "$(grep ^POSTGRES_USER= $ENV_FILE | cut -d= -f2)" -d "$(grep ^POSTGRES_DB= $ENV_FILE | cut -d= -f2)" \
  -c "select count(*) from users" -c "select count(*) from ranges" -c "select version_num from alembic_version"
curl -fsS https://$DOMAIN/api/health/deep
```

- If the deployed code is newer than the backup's `source_git_rev`, run migrations
  ([upgrade.md](upgrade.md#upgrade-t-0), step 4).
- If the DB passwords changed since the backup, `globals.sql` put the old hashes back:
  restore that backup's secrets too (`restore-secrets.sh --force`, then the installer), or
  rotate them again (`playbooks/rotate-secret.yml`).
- Users must log in again (Redis sessions/rate limits are empty).

## OpenSearch

`compose.prod.yml` sets no `path.repo` and mounts no snapshot volume, so by default there is
nowhere to snapshot to and telemetry is **not** backed up. The decision: scores, AARs,
enrollments and course records are in PostgreSQL/MinIO and are backed up; raw telemetry is
high-volume, short-lived exercise evidence and is accepted as lost if the OpenSearch volume
is lost. To include it:

1. Give OpenSearch a repository (shared FS: add `path.repo=/mnt/snapshots` and a volume; or
   the `repository-s3` plugin) and register it:
   `PUT _snapshot/tn_snapshots {"type":"fs","settings":{"location":"/mnt/snapshots"}}`.
2. Set `OPENSEARCH_SNAPSHOT_REPO=tn_snapshots` in `backup.env`; with the security plugin on
   (the default), put `OPENSEARCH_SNAPSHOT_AUTH=user:pass` in the env file. `backup.sh` calls
   `https://localhost:9200` inside the container and verifies the node certificate against
   the internal CA (`OPENSEARCH_SNAPSHOT_CACERT`, default `config/certs/ca.pem`); there is no
   `curl -k`. The credentials reach curl on stdin, not its command line.
3. `backup.sh` then takes `tn-<timestamp>` snapshots and records them in the manifest.
   Restore: close the target indices, then
   `POST _snapshot/tn_snapshots/<snapshot>/_restore?wait_for_completion=true`.
   Snapshots live in the repository, not in the backup directory; retain them there.

## Drill

```bash
scripts/backup/drill.sh                       # unique project name, torn down afterwards
DRILL_KEEP=1 scripts/backup/drill.sh          # leave it up to inspect
```

It brings up `postgres` + `minio` from the real `compose.prod.yml` in a throwaway compose
project (named volumes instead of the `/srv/truenorth` binds, renamed networks, small memory
settings), seeds the app/keycloak/lrs databases (each table owned by its own role) and three
buckets, backs up, `down -v`, restores, and requires identical per-table row counts and
owners, identical bucket list and object count, the escrow to decrypt back to the exact env
file, `TN_SECRETS_KEY` to appear nowhere in clear, a second backup to succeed, and a backup
with a missing env file to exit non-zero with an alert. Then it plays a **reinstalled host**:
`down -v`, fresh secrets and env, stack up empty; `restore-secrets.sh` must refuse without
`--force` and then write the escrowed secrets and env back exactly; the services are recreated
with them, `restore.sh` runs, the state must equal the seed, and the restored database
passwords must authenticate over TCP while the fresh ones must not. It refuses the project
names `truenorth`, `docker` and `tn-r0-pg`. Never point it at a live stack.

Drill result 2026-10-08 with the reinstall step (macOS, project `tn-drill-installer-v1`):
PASS — same counts as below, before backup == after restore == after the reinstall restore.

Drill result 2026-10-08 (macOS, Docker 29 / compose 5.5): PASS — `drill_app` ranges 1000,
enrollments 2500; `keycloak.user_entity` 137 (owner keycloak); `lrs.xapi_statement` 420
(owner lrs); 3 buckets (one empty), 32 objects; identical before and after.

Quarterly: restore the latest production backup onto a staging host and run the "After"
checks above. Record the date and counts here.
