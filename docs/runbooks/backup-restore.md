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
| `moodle/moodledata.tar` | only with the installer's Moodle (`tn_moodle_enabled`; `MOODLE_COMPOSE_FILE` in backup.env): its files, without caches, sessions and temp, archived through a throwaway container of its image. Its database is one of the `postgres/<db>.dump` files (`moodle`). See [moodle.md](moodle.md). |
| `manifest.json` | databases and sizes, bucket/object counts, the OpenSearch snapshot's repository and name (or `not-backed-up`), Moodle's files (`backed-up`, `not-installed` or `not-configured`), escrow status, compose project, git commit |
| `SHA256SUMS` | checksums of everything above; verified at the end of the backup and before any restore |

Not included, on purpose:

- **Redis** — Celery broker, cache, rate-limit counters. Restoring a stale queue would replay
  provisioning tasks. After a restore Redis starts empty; that is correct.
- **OpenSearch indices, as files** — telemetry is backed up as an OpenSearch snapshot that
  stays in the snapshot repository (`/srv/truenorth/opensearch-snapshots`), not in the backup
  directory; the manifest names it. See [OpenSearch](#opensearch).
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
OPENSEARCH_SNAPSHOT_REPO=tn_snapshots   # tn_opensearch_snapshot_repo; absent when that is ""
OPENSEARCH_SNAPSHOT_KEEP=14             # tn_opensearch_snapshot_keep
# OPENSEARCH_URL=http://localhost:9200  # only with tn_opensearch_disable_security (lab)
# MOODLE_COMPOSE_FILE=.../compose.moodle-prod.yml   # only with tn_moodle_enabled
# MOODLE_ENV_FILE=/srv/truenorth/config/moodle-default.env

# Optional, add by hand
# DAILY_KEEP=7 WEEKLY_KEEP=4 MONTHLY_KEEP=12
# S3_BUCKET=s3://truenorth-backups  S3_ENDPOINT=...  S3_ENCRYPT=1  GPG_RECIPIENT=...
# BACKUP_WEBHOOK_URL=https://...    # extra alert channel; stderr + syslog always alert
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
Exit codes: `1` failed (nothing kept), `3` data kept but no escrow, `4` offsite upload failed,
`5` data and secrets kept but the OpenSearch telemetry snapshot (or its pruning) failed — the
manifest's `opensearch` entry says `"status":"failed"` and why. `3` wins when both apply; both
alert. `cron-backup.sh` treats `3` and `5` alike: it uploads and rotates the backup and passes
the code through.

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

OpenSearch's security index is not in the snapshot (only telemetry indices are): on a
rebuilt host it was initialised from whatever `internal_users.yml` held at first start. If
that was before the secrets were restored, load the restored passwords with
`playbooks/rotate-secret.yml -e tn_rotate=opensearch_admin_password` (and
`opensearch_dashboards_password`), or wipe `/srv/truenorth/opensearch` before the
installer's first run.

To get telemetry back too, copy the snapshot repository directory back to
`/srv/truenorth/opensearch-snapshots` (owner `1000:1000`) before step 3. The installer's
`70-telemetry` registers it, and OpenSearch then lists the snapshots it holds.

## Restore

Pre-checks:

1. Pick the backup: `ls -1d $BACKUP_DIR/*/truenorth-backup-* | sort | tail`. Read its
   `manifest.json`.
2. On a new host, restore the secrets first ("Rebuilt host", above). To read just the env
   file: `scripts/backup/escrow-open.sh <backup> backup-escrow.key <out>` (run where the
   private key is).
3. The stack must be up (`postgres` and `minio` at least, and `opensearch` when the manifest
   names a snapshot) — e.g. after the installer ran with the restored secrets.

Run:

```bash
cd /srv/truenorth/app
set -a; . /srv/truenorth/config/backup.env; set +a
scripts/backup/restore.sh $BACKUP_DIR/daily/truenorth-backup-20261008T021700Z
#   [--force]          skip the "type the project name" prompt
#   [--no-restart]     leave the app services stopped afterwards
#   [--skip-postgres | --skip-minio | --skip-opensearch | --skip-moodle]
```

With Moodle (`MOODLE_COMPOSE_FILE` set and the backup holding `moodle/moodledata.tar`), the
Moodle node is stopped with the rest, its database comes back with the others, moodledata
is replaced by the archive's, and the node starts after the platform.

What it does: verifies `SHA256SUMS` → if the manifest names an OpenSearch snapshot, checks
that it is in its repository, and **refuses before stopping anything** if it is not (pruned
beyond `OPENSEARCH_SNAPSHOT_KEEP`, or the repository directory was not copied back; re-run
with `--skip-opensearch` to restore without telemetry; a backup whose snapshot had *failed*
(`backup.sh` exit 5) restores everything else with a warning, no flag needed) → stops every running service except
`postgres`, `minio` and (for a snapshot) `opensearch` → applies `globals.sql` (existing roles
are kept; their attributes and password hashes are reset to the backup's) → for each
`postgres/<db>.dump`: `DROP DATABASE … WITH (FORCE)` then `pg_restore --create
--exit-on-error` → for each bucket: `mc mb --ignore-existing` then `mc mirror --overwrite`
(objects created after the backup are left in place) → deletes every telemetry index (names
not starting with `.`) and restores the snapshot's, aliases included
(`include_global_state: false`: templates, pipelines and the security index stay as the
installer made them; telemetry written after the backup is gone, as with the databases) →
starts the services it stopped.

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

Installed by default (single-node compose):

| Piece | Where |
|---|---|
| Repository directory | `/srv/truenorth/opensearch-snapshots`, owner `1000:1000`, mode `0750` (`10-base`) |
| `path.repo=/usr/share/opensearch/snapshots` and the `os-snapshots` bind | `compose.prod.yml` |
| Repository `tn_snapshots` (`fs`, compressed) registered | `70-telemetry` (`telemetry.pipelines.bootstrap`; idempotent, fails the play if OpenSearch cannot write there) |
| `OPENSEARCH_SNAPSHOT_REPO`, `OPENSEARCH_SNAPSHOT_KEEP` | `backup.env` (`30-config`) |
| `OPENSEARCH_SNAPSHOT_AUTH=admin:…` | the env file (`30-config`), read by the scripts, never passed on a command line |

Each `backup.sh` takes snapshot `tn-<timestamp>` of the telemetry indices (names not
starting with `.`; no global state, no security index) inside the opensearch container,
against `https://localhost:9200`, verifying the node certificate against the internal CA
(`OPENSEARCH_SNAPSHOT_CACERT`); there is no `curl -k`. The credentials reach curl on stdin.
Then it deletes all but the newest `OPENSEARCH_SNAPSHOT_KEEP` (14) `tn-*` snapshots: every
snapshot pins the indices it holds, so without a cap the repository would keep telemetry the
90-day ISM policy has already deleted. An older backup whose snapshot was pruned restores
without telemetry (`--skip-opensearch`).

**A failed snapshot never costs the data.** If OpenSearch is down, the snapshot is not
`SUCCESS`, or pruning fails, the database dumps, buckets and escrow are still completed and
kept; the manifest records `{"status":"failed", …, "error": …}` (or, when only pruning failed,
the good snapshot), an `[ALERT]` is raised, and `backup.sh` exits `5`. `restore.sh` restores
such a backup in full except telemetry, with a warning; no flag is needed. Look at the
OpenSearch container and the error in the manifest, then let the next night's backup take a
snapshot (or run `backup.sh` by hand).

**Off the host.** Snapshots live in `/srv/truenorth/opensearch-snapshots`, not in the backup
directory, and `cron-backup.sh`'s S3 upload ships only the backup directory. Copy the
repository directory off the host with the backups (it is incremental: `rsync -a` is cheap),
and copy it back before restoring onto a rebuilt host ("Rebuilt host").

```bash
# The repository and its snapshots, from the host
. /srv/truenorth/config/backup.env
docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -T opensearch sh -c \
  'curl -sS --cacert config/certs/ca.pem -u "$OPENSEARCH_USER:$OPENSEARCH_PASS" \
     "https://localhost:9200/_cat/snapshots/tn_snapshots?v"'
```

To leave telemetry out of backups: `tn_opensearch_snapshot_repo: ""` and re-run `30-config`
(the manifest then records `not-backed-up`). Scores, AARs, enrollments and course records are
in PostgreSQL/MinIO either way.

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

OpenSearch (on by default, `DRILL_OPENSEARCH=0` to leave it out): `opensearch` runs beside
them with the snapshot repository bind-mounted from the drill's work directory (so it
outlives `down -v`, like the host directory), registered on every start as `70-telemetry`
does. A per-range index and a rollover index behind its write alias are part of the compared
state; an index written after the backup must be gone after the restore. Finally a third
backup with `OPENSEARCH_SNAPSHOT_KEEP=2` must prune the first snapshot, and `restore.sh` of
the first backup must then refuse without stopping any service. Last, a backup into a
repository nobody registered must exit `5` with an alert and keep its data, and restoring it
must give back the seeded state without `--skip-opensearch`. The drill runs OpenSearch
**without** its security plugin (it has no internal CA), so the TLS and basic-auth path is
exercised on an installed host, not here.

Drill result 2026-10-09 with OpenSearch (macOS, Docker 29.8, OpenSearch 2.13.0, project
`tn-drill-osnap-1`): PASS — `range-drill-0001` 37 documents, `exercise-events-000001` 12
documents behind alias `exercise-events`, identical before backup, after restore and after
the reinstall restore (shards 2/2, 0 failed); pruned at KEEP=2; the pruned backup refused.
Re-run the same day with the failed-snapshot step (project `tn-drill-osnap-5`): PASS — the
backup into an unregistered repository exited 5 with an alert, its manifest recorded the
failure, and restoring it gave back the seeded state with a warning.

Drill result 2026-10-08 with the reinstall step (macOS, project `tn-drill-installer-v1`):
PASS — same counts as below, before backup == after restore == after the reinstall restore.

Drill result 2026-10-08 (macOS, Docker 29 / compose 5.5): PASS — `drill_app` ranges 1000,
enrollments 2500; `keycloak.user_entity` 137 (owner keycloak); `lrs.xapi_statement` 420
(owner lrs); 3 buckets (one empty), 32 objects; identical before and after.

Quarterly: restore the latest production backup onto a staging host and run the "After"
checks above. Record the date and counts here.
