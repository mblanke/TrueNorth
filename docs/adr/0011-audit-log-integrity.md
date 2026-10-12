# ADR 0011: Audit log integrity — append-only table, signed daily export, retention

- Status: accepted (v1.2.0, security track S)
- Date: 2026-10-12
- Numbering: 0008 is reserved by an open branch; 0010 was the last one used.

## Context

`audit_logs` is TrueNorth's record of state changes (`app/models.py` `AuditLog`: id,
timestamp, user, tenant, action, resource type and id, detail). Before v1.2.0 nothing
stopped a writer, an operator with database access, or a bug from changing or deleting
rows; there was no export, no retention and no way to show that the log had not been
edited. `docs/security.md` claimed an append-only table, a daily export to immutable
storage and 90 d / 365 d / 7 y retention. None of it existed.

The API and the worker connect as the database owner (`POSTGRES_USER`, also the
superuser in the shipped compose file). `REVOKE` on that role achieves nothing: it can
disable triggers and set `session_replication_role`.

## Decision

1. **Append-only by trigger** (migration `b7c8d9e0f1a2`, PostgreSQL only):
   - `UPDATE` on `audit_logs` is refused; so is `TRUNCATE` of `audit_logs` and
     `audit_exports`; `audit_exports` rows are never updated or deleted.
   - Every inserted row's `timestamp` is set to `clock_timestamp()`, whatever the writer
     sent, so nothing can be backdated into a day that has already been exported.
2. **One DELETE path**, the retention job's. The trigger lets a row go only when all hold:
   - the transaction set `tn.audit_purge = on` (`set_config(..., true)`: transaction-local);
   - the row is at least **30 days** old (UTC days; hard-coded in the trigger, no setting
     lowers it);
   - its UTC day has an `audit_exports` row **and** an `audit_export_checks` row for that
     export, with the same `manifest_sha256`, less than a day old (`checked_at` stamped by
     the database clock).
   The job writes that check only after re-verifying the day, in the same transaction as
   the delete: every manifest is signed by a key id the database knows (the worker's
   current key or one `audit_exports` recorded), its bytes hash to the recorded
   `manifest_sha256`, its file to `file_sha256`, the summed `row_count` equals the table's
   count and the exported ids are exactly the table's ids. It deletes a whole day in one
   transaction, writes an `audit.purge` row (day, row count, manifest hashes) and stops at
   the first day that fails a check. While the application connects as the database owner
   these rows are evidence the job wrote, not a barrier against the owner (residual risk).
3. **Signed daily export** (`worker/audit_tasks.py`, beat 00:45 UTC): for every UTC day
   before today not yet exported (so a stopped worker catches up), a gzipped JSON-lines
   file (every column, ordered by timestamp then id), a manifest (day, seq, row count,
   first/last id, file name and SHA-256, previous manifest's SHA-256, key id, time) and
   an Ed25519 signature over the manifest's exact bytes. All three are read back and
   verified before the `audit_exports` row is written. An empty day gets a manifest too.
   The manifests form **one hash chain** in the order they were written; a missing,
   reordered or edited manifest breaks it. A recount of the day before yesterday catches
   rows that committed after their day was exported (long transactions): they go into a
   supplement (`seq` + 1), with an ERROR logged.
4. **Archive** behind an adapter (ADR 0001): `worker/audit_archive/`, registry by
   `AUDIT_ARCHIVE_BACKEND`, default `filesystem` (`AUDIT_ARCHIVE_DIR`, mounted from
   `AUDIT_ARCHIVE_PATH`, default `/var/lib/truenorth/audit-archive`). Writes are
   temp-file, fsync, rename; files 0640; a name is never overwritten with other bytes.
   `backup.sh` copies the archive into every backup and verifies every signature and
   hash with OpenSSL 3, walks the chain, and (when the database has `audit_exports`)
   requires every recorded export in the archive with its exact hash and every signing key
   to be one the database recorded; exit 6 otherwise, which the nightly job still uploads
   and rotates, with a critical `AuditArchiveTampered` alert. `restore.sh` restores the
   archive before the databases and afterwards moves every export the restored
   `audit_exports` does not list to `quarantine/<time>/` (kept, recorded in `RESTORE.json`
   and an `audit.archive_quarantine` row), so the export continues from the restored
   database. The restore drill re-verifies the chain and exercises that quarantine.
5. **Key handling.** `AUDIT_SIGNING_SEED`, an installer-generated secret
   (`audit_signing_seed`, persisted under `config/secrets`, escrowed by `backup.sh`), is
   passed to the worker that runs the export (worker-scenario) and to no other service.
   Key = Ed25519 from HKDF-SHA256(seed, info `truenorth-audit-export-v1`); `key_id` = first
   16 hex characters of SHA-256 of the raw public key. No fallback key: without a seed of
   32+ characters nothing is exported or purged. The public key is written to
   `keys/<key_id>.pub.pem` in the archive; the installer prints its fingerprint at the end
   of the install so the operator records it **out of band**. Rotation
   (`rotate-secret.yml -e tn_rotate=audit_signing_seed`) starts a new `key_id`; old public
   keys stay in `keys/`, and the chain continues across the change.
6. **Retention defaults.** `AUDIT_HOT_RETENTION_DAYS` 90 (floor 30; 0 turns purging off).
   Archive files are never deleted by TrueNorth; `AUDIT_ARCHIVE_RETENTION_DAYS` is for an
   operator's own pruning and is not acted on. Login events are Keycloak's (event store,
   90 days, enabled by 60-keycloak), not `audit_logs`.
7. **Tenancy.** Exports are platform-wide files with every tenant's rows. No API serves
   them in v1.2.0. If a download endpoint is added: the whole file to the platform
   administrator only; a tenant administrator gets a server-filtered, re-signed extract
   of their tenant, never the raw file.
8. `audit_events` (migration `d4e5f6a1b2c3`) has no model and no writer. It is left alone
   (not guarded) so a later cleanup can drop it.

## Residual risk (stated plainly so AU-9 is not overclaimed)

- **R3, the owner credential.** The API and worker connect as the database owner, which
  can disable these triggers, and the signing seed is on the same host. Within v1.2.0 the
  defence against that is **detection, not prevention**: the hash chain, the public-key
  fingerprint recorded out of band, and verified backups kept off the host. An attacker
  with root on the host can rewrite rows and re-sign a new chain; comparing `key_id` with
  the recorded fingerprint, and the archive with an older off-host backup, shows it.
- **Prevention is v1.3:** a separate non-owner database role for the API and worker with
  `UPDATE`/`DELETE`/`TRUNCATE` revoked on `audit_logs`, and with **no `INSERT`** on
  `audit_exports` or `audit_export_checks` either (otherwise a compromised application
  could record a fake export and check and purge through the trigger; only a dedicated
  exporter role writes them), plus WORM storage (MinIO object lock) as a second archive
  backend.
- The filesystem archive is on the worker host's disk; it is as durable as that disk
  until the nightly backup copies it.
- Helm: the chart does not yet mount the archive or pass the seed; compose is the
  supported deployment for v1.2.0.

## Consequences

- No current code path updates or deletes `audit_logs` (checked: writers only insert,
  users are soft-deleted, there is no tenant delete, ADR 0010 forbids cascades on history,
  the foreign keys have no ON DELETE, restore is DDL), so the trigger breaks nothing.
- Tests that need old rows on PostgreSQL disable the insert stamp as the table owner
  inside the test (`tests/api/test_audit_append_only_postgres.py`).
- Deep health reports `audit_export` degraded when the latest export is older than
  the day before yesterday while older rows exist.
