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

from . import audit_signing
from .audit_archive import get_archive
from .audit_export import DEFAULT_HOT_DAYS, FLOOR_DAYS  # noqa: F401 — re-exported

# The work lives in audit_jobs.py (no Celery: the restore drill imports it).
from .audit_jobs import recount, run_export, run_retention, today_utc  # noqa: F401 — recount re-exported
from .base_tasks import ReliableTask
from .celery_app import app
from .task_plumbing import db_session as _db_session


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
