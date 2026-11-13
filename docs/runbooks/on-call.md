# Runbook — on-call

## Rota

- One primary and one secondary, weekly, handover Monday 09:00 local. Keep the rota where
  the team keeps its calendar; fill in the names and contact numbers there, not in git.
- Primary acknowledges pages within 15 min (SEV1) / 1 h (SEV2). Secondary takes over if the
  primary has not acknowledged in that time.
- Escalation after 30 min without progress on a SEV1: platform lead, then the product owner.

## What pages

| Signal | Source | Sev |
|---|---|---|
| API `/health/ready` failing > 5 min | api container healthcheck (`compose.prod.yml`: `curl /health/ready`; externally `/api/health/ready`) — no blackbox exporter or API-down alert ships | SEV1 |
| PostgreSQL down (no alert ships — `dc ps postgres`), OpenSearch red (`OpenSearchClusterRed`) | Prometheus alerts (`operations.md`) | SEV1 / SEV2 |
| Backup exit ≠ 0, or no new backup in 26 h | `journalctl -t truenorth-backup`, `backup.log`, Prometheus `BackupFailed` / `BackupStale`, optional `BACKUP_WEBHOOK_URL` | SEV3 (SEV2 if two nights running; SEV1 if a datastore is degraded) |
| Backup drill failed | GitHub Actions `Backup drill` (nightly) | SEV3 — the scripts or the compose file changed under them |
| Disk > 80 % on `/srv/truenorth` | node-exporter | SEV2 |

The backup signal is already in Prometheus: `backup.sh` writes `truenorth_backup.prom`, and
`alerts.yml` fires `BackupFailed`, `BackupIncomplete`, `AuditArchiveTampered` and `BackupStale`
(no good backup for 26 h). Delivery is Alertmanager's, and out of the box it routes everything to
a null receiver — wire that into your paging. The scripts also write `[ALERT]` lines to syslog
with tag `truenorth-backup` and, optionally, `BACKUP_WEBHOOK_URL`.

## Start of shift

1. Read the previous handover note and any open incident.
2. `ls -1d /srv/truenorth/backups/*/truenorth-backup-* | sort | tail -3` — last night's
   backup exists; `grep -c ALERT /srv/truenorth/logs/backup.log` did not grow.
3. Grafana "TrueNorth Range — Overview": error rate, queue depth, disk.
4. Confirm you can reach the host, the vault (installer secrets) and the escrow key custodian.

## During the shift

- Follow [incident.md](incident.md) for anything paged.
- Planned changes go through [upgrade.md](upgrade.md); never upgrade on the last day of a shift.
- Do not run `scripts/backup/drill.sh` on the production host; it is for CI and workstations.

## Communication

- Users: the platform banner / course announcement channel. SEV1: within 15 min, then every
  30 min. Say what is affected, what Students and instructors should do, next update time.
- Team: the incident channel; one thread per incident; IC posts the timeline.
- Planned work: announce 2 working days ahead with the window and what is unavailable.

## Handover

Write: open incidents and their state, anything changed during the shift (with times), any
backup that did not complete, and anything the next person must do or watch.
