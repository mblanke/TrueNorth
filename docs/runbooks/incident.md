# Runbook — incident response

For alert-specific steps see [operations.md → Alert Response Procedures](../operations.md#alert-response-procedures);
for range lease problems, [operations.md → Range leases](../operations.md#range-leases-abandon-and-recovery).
This page is the process around them.

## Severity

| Sev | Meaning | Examples | Response |
|---|---|---|---|
| SEV1 | Training stopped for everyone, or data at risk | API down; PostgreSQL down or corrupt; secrets exposed; backups failing and a datastore is degraded | Page now, all hands, updates every 30 min |
| SEV2 | A major function down, workaround exists | Provisioning failing; Keycloak login failing for one group; telemetry/scoring stalled | Page in hours, updates hourly |
| SEV3 | Degraded, few users | One worker queue backed up; a dashboard broken; a nightly backup failed once (exit 1/3/4) | Next business day |

When in doubt, pick the higher severity and lower it later.

## First 15 minutes

1. **Acknowledge** the page; open an incident record (date, sev, IC = you).
2. **Look**, don't fix yet:

   ```bash
   dc() { docker compose -f /srv/truenorth/app/infra/platform/docker/compose.prod.yml \
                         --env-file /srv/truenorth/config/.env.production "$@"; }
   dc ps                                     # anything restarting/unhealthy?
   curl -fsS https://$DOMAIN/api/health/deep # per-dependency status
   dc logs --since 30m api worker-provision worker-scenario | tail -200
   df -h /srv/truenorth                      # full data disk is a common root cause
   journalctl -t truenorth-backup --since -2d
   ```
3. **Stabilise**: stop the bleeding with the smallest reversible action (restart one
   service, pause a queue, put nginx in maintenance). Write every action and its time in the
   record.
4. **Protect data before risky actions.** Before anything that touches a datastore
   (restore, manual SQL, volume work), take a backup if PostgreSQL is still readable:
   `scripts/backup/backup.sh` ([backup-restore.md](backup-restore.md)).
5. **Communicate** ([on-call.md](on-call.md#communication)).

## Common decisions

- **Data loss / corruption** → stop writers (`dc stop api worker-provision worker-scenario
  worker-telemetry`), pick the last good backup, follow [backup-restore.md](backup-restore.md#restore).
  State the data-loss window (backup time → incident) in the record.
- **Bad release** → [upgrade.md → Roll back](upgrade.md#roll-back). Code rollback first;
  data rollback only if a migration damaged data.
- **Secret exposed** (env file, `TN_SECRETS_KEY`, DB password, escrow private key, LTI
  key) → SEV1; rotate per [key-rotation.md](key-rotation.md); check `docs/` and backups for
  copies; record who could have seen it.
- **Stuck jobs / inconsistent state** → see the `tn-job-lifecycle` and
  `tn-incident-diagnosis` skills (`SKILLS/`), and the lease runbook in operations.md.

## Close

1. Confirm recovery: health deep green, a small range provisions, a Student can log in and
   open a course, the next backup succeeds.
2. Within 5 working days, a blameless review in `docs/hardening/` (or the team's tracker):
   timeline, impact (who, how long, data lost), root cause, what made it worse, actions with
   owners. A runbook step that was wrong is fixed in the same week.
