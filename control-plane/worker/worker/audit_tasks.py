"""Audit log export and retention (ADR 0011). Beat runs both daily (celery_app.py).

``export_audit_days`` (00:45 UTC): every UTC day before today that has no export yet,
from the day after the last export (or the first audit row) up to yesterday, so a
stopped worker catches up. Then a recount of the day before yesterday: rows that
committed after that day was exported (a long transaction) go into a supplement
(seq + 1), and an ERROR is logged.

``enforce_audit_retention`` (02:15 UTC): rows older than AUDIT_HOT_RETENTION_DAYS (default
90, never under 30; 0 turns purging off) are deleted day by day, each only when its
exports exist, still verify, and hold exactly the rows in the table. The delete runs with
the transaction-local setting ``tn.audit_purge=on``, the one path the database trigger
lets through (migration b7c8d9e0f1a2), and writes an ``audit.purge`` row. It stops at the
first day that fails a check. Archive files are never deleted here.

Neither task generates a key: without AUDIT_SIGNING_SEED nothing is exported or purged.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from . import audit_signing
from .audit_archive import AuditArchive, get_archive
from .audit_export import (
    AuditExportError,
    chain_head,
    day_bounds,
    day_count,
    day_rows,
    exported_ids,
    exports_for,
    manifest_name,
    verify_manifest,
    write_export,
)
from .base_tasks import ReliableTask
from .celery_app import app
from .tables import audit_exports, audit_logs
from .task_plumbing import db_session as _db_session

logger = logging.getLogger("truenorth.worker.audit")

DEFAULT_HOT_DAYS = 90
FLOOR_DAYS = 30  # the database trigger refuses younger rows whatever this says


def hot_retention_days() -> int:
    """AUDIT_HOT_RETENTION_DAYS, clamped to at least 30; 0 disables purging."""
    raw = os.getenv("AUDIT_HOT_RETENTION_DAYS", str(DEFAULT_HOT_DAYS)).strip() or str(DEFAULT_HOT_DAYS)
    days = int(raw)
    if days <= 0:
        return 0
    return max(days, FLOOR_DAYS)


def today_utc() -> date:
    return datetime.now(UTC).date()


def _first_row_day(db) -> date | None:
    first = db.execute(sa.select(sa.func.min(audit_logs.c.timestamp))).scalar()
    if first is None:
        return None
    return (first.replace(tzinfo=UTC) if first.tzinfo is None else first.astimezone(UTC)).date()


def run_export(db, archive: AuditArchive, key: audit_signing.SigningKey, today: date) -> list:
    """Export every missing day before ``today``, then recount today-2. Returns what was written."""
    written = []
    last = db.execute(sa.select(sa.func.max(audit_exports.c.day))).scalar()
    start = (last + timedelta(days=1)) if last else _first_row_day(db)
    if start is not None:
        day = start
        while day < today:
            written.append(write_export(db, archive, key, day, 0, day_rows(db, day), chain_head(db)))
            day += timedelta(days=1)
    supplement = recount(db, archive, key, today - timedelta(days=2))
    if supplement is not None:
        written.append(supplement)
    return written


def recount(db, archive: AuditArchive, key: audit_signing.SigningKey, day: date):
    """Rows of an exported ``day`` that are not in its files go into a supplement."""
    exports = exports_for(db, day)
    if not exports:
        return None
    live = day_count(db, day)
    exported = sum(e.row_count for e in exports)
    if live == exported:
        return None
    if live < exported:
        logger.error("AUDIT_EXPORT_MISMATCH %s: %d rows in the table, %d exported (rows went missing)",
                     day, live, exported)
        return None
    have = exported_ids(archive, db, day, key)
    late = [r for r in day_rows(db, day) if str(r._mapping["id"]) not in have]
    logger.error("AUDIT_EXPORT_MISMATCH %s: %d rows committed after the export; writing supplement seq %d",
                 day, len(late), exports[-1].seq + 1)
    return write_export(db, archive, key, day, exports[-1].seq + 1, late, chain_head(db))


def run_retention(db, archive: AuditArchive, key: audit_signing.SigningKey, today: date) -> list[dict]:
    """Purge whole days older than the hot window, each only after its export checks out."""
    days = hot_retention_days()
    if days == 0:
        logger.info("audit retention: AUDIT_HOT_RETENTION_DAYS=0, purging is off")
        return []
    cutoff = today - timedelta(days=days)
    purged: list[dict] = []
    day = _first_row_day(db)
    while day is not None and day < cutoff:
        live = day_count(db, day)
        if live:
            reason = _purge_refusal(db, archive, key, day, live)
            if reason:
                logger.error("AUDIT_RETENTION_STOPPED at %s: %s", day, reason)
                break
            purged.append(_purge_day(db, day, live))
        day += timedelta(days=1)
    return purged


def _purge_refusal(db, archive, key, day: date, live: int) -> str | None:
    exports = exports_for(db, day)
    if not exports:
        return "no export for this day"
    if sum(e.row_count for e in exports) != live:
        return f"{live} rows in the table, {sum(e.row_count for e in exports)} exported"
    try:
        for e in exports:
            verify_manifest(archive, manifest_name(day, e.seq), key)
        ids = exported_ids(archive, db, day, key)
    except AuditExportError as exc:
        return str(exc)
    if ids != {str(r._mapping["id"]) for r in day_rows(db, day)}:
        return "the rows in the table are not the rows in the export"
    return None


def _purge_day(db, day: date, live: int) -> dict:
    start, end = day_bounds(day)
    shas = [e.manifest_sha256 for e in exports_for(db, day)]
    if db.get_bind().dialect.name == "postgresql":
        # Transaction-local: only this transaction's DELETE passes the trigger.
        db.execute(sa.select(sa.func.set_config("tn.audit_purge", "on", True)))
    deleted = db.execute(
        audit_logs.delete().where(audit_logs.c.timestamp >= start, audit_logs.c.timestamp < end)
    ).rowcount
    if deleted != live:
        db.rollback()
        raise AuditExportError(f"purge of {day}: deleted {deleted}, expected {live}; rolled back")
    detail = {"rows": deleted, "manifest_sha256": shas}
    db.execute(audit_logs.insert().values(
        id=uuid.uuid4(), timestamp=datetime.now(UTC), user_id=None, tenant_id=None, action="audit.purge",
        resource_type="audit_logs", resource_id=day.isoformat(), detail=json.dumps(detail, sort_keys=True),
    ))
    db.commit()
    logger.info("audit retention: purged %d rows of %s (exported, verified)", deleted, day)
    return {"day": day.isoformat(), **detail}


# ── Celery tasks (beat: worker/celery_app.py) ─────────────────────────
@app.task(name="worker.audit_tasks.export_audit_days", base=ReliableTask, bind=True)
def export_audit_days(self) -> dict:
    key = audit_signing.load()  # raises without a seed: nothing unsigned is ever written
    with _db_session() as db:
        written = run_export(db, get_archive(), key, today_utc())
    return {"exported": [f"{w.day}#{w.seq} ({w.row_count})" for w in written], "key_id": key.key_id}


@app.task(name="worker.audit_tasks.enforce_audit_retention", base=ReliableTask, bind=True)
def enforce_audit_retention(self) -> dict:
    key = audit_signing.load()
    with _db_session() as db:
        purged = run_retention(db, get_archive(), key, today_utc())
    return {"purged": purged}
