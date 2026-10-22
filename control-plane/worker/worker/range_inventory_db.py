"""Database reads and writes of range certification S5 (ADR 0013 §5, issue #243).

Through the generated table mirror (tables.py), SQLAlchemy Core only: no raw SQL in the
worker (ADR 0003). Kept out of db_ops.py and tasks.py (per-section modules).

* ``save_check`` / ``load_check``: the latest teardown verification or drift check of a
  range (``range_inventory_checks``, one row per range and kind; the API reads it).
* ``drift_ranges`` / ``snapshot_rows``: what the periodic drift check reads.
* ``append_entry``: one Range Certification Record entry (``drift``), appended to the
  range's newest record the way the API's ``range_certification.service.append`` does it
  (the record row locked, ``seq`` = last + 1), and only when the section's latest entry does
  not already say exactly this. Entries are never updated or deleted.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from .tables import (
    range_certification_entries,
    range_certification_records,
    range_inventory_checks,
    range_leases,
    range_operations,
    range_snapshots,
    ranges,
)

checks = range_inventory_checks
LIVE_STATES = ("ready", "running", "stopped")  # a built range nothing is changing
IN_FLIGHT = ("pending", "dispatched", "held")  # app/range_ops/models.py
BUSY_SNAPSHOT_STATES = ("creating", "restoring")


def _now() -> datetime:
    return datetime.now(UTC)


def _plain(value):
    """JSON-normalised (what a JSON column gives back), so equal content compares equal."""
    return json.loads(json.dumps(value, default=str))


def load_check(db, range_id: str, kind: str) -> dict | None:
    row = db.execute(sa.select(checks.c.status, checks.c.summary, checks.c.report)
                     .where(checks.c.range_id == range_id, checks.c.kind == kind)).first()
    if row is None:
        return None
    report = row[2] if isinstance(row[2], dict) else json.loads(row[2]) if isinstance(row[2], str) else {}
    return {"status": row[0], "summary": row[1], "report": report}


def save_check(db, range_id: str, kind: str, status: str, summary: str, report: dict) -> bool:
    """Record the latest check. Returns whether what it found changed (``changed_at``)."""
    report = _plain(report)
    now = _now()
    current = load_check(db, range_id, kind)
    changed = current is None or (current["status"], current["summary"], _plain(current["report"])) != (
        status, summary[:500], report)
    if current is None:
        tenant = db.execute(sa.select(ranges.c.tenant_id).where(ranges.c.id == range_id)).scalar()
        db.execute(sa.insert(checks).values(
            id=uuid.uuid4(), range_id=range_id, tenant_id=tenant, kind=kind, status=status, summary=summary[:500],
            report=report, checked_at=now, changed_at=now))
        return True
    values = {"checked_at": now}
    if changed:
        values.update(status=status, summary=summary[:500], report=report, changed_at=now)
    db.execute(sa.update(checks).where(checks.c.range_id == range_id, checks.c.kind == kind).values(**values))
    return changed


def range_state(db, range_id: str) -> str | None:
    state = db.execute(sa.select(ranges.c.state).where(ranges.c.id == range_id)).scalar()
    return getattr(state, "value", state)


def _now_in_db(db):
    return sa.func.now() if db.get_bind().dialect.name == "postgresql" else _now()


def _leased(db):
    return sa.select(range_leases.c.range_id).where(range_leases.c.expires_at > _now_in_db(db))


def _in_flight():
    return sa.select(range_operations.c.range_id).where(range_operations.c.status.in_(IN_FLIGHT))


def drift_ranges(db) -> list:
    """(id, name, provisioner_output, provisioner_backend) of every built range that is not
    being changed now: ready, running or stopped, no operation in flight and no worker
    holding its lease (a build, restore, power change or teardown in progress would show as
    drift)."""
    stmt = (sa.select(ranges.c.id, ranges.c.name, ranges.c.provisioner_output, ranges.c.provisioner_backend)
            .where(sa.cast(ranges.c.state, sa.String).in_(LIVE_STATES), ranges.c.deleted_at.is_(None),
                   ranges.c.id.not_in(_leased(db)), ranges.c.id.not_in(_in_flight())))
    return db.execute(stmt).fetchall()


def db_now(db):
    """The database's clock (every writer stamps ``updated_at`` with it): a sweep's start."""
    if db.get_bind().dialect.name != "postgresql":
        return _now()
    return db.execute(sa.select(sa.func.now())).scalar()


def unsettled(db, range_id: str, since) -> str | None:
    """Why a drift reading taken since ``since`` must not be recorded for the range, or None:
    it is no longer idle (state, lease, an operation in flight), or the range or one of its
    snapshots changed while it was read (a snapshot taken, restored or deleted then would
    show as false drift in the append-only record)."""
    state = range_state(db, range_id)
    if state not in LIVE_STATES:
        return f"the range is {state} now"
    if db.execute(_leased(db).where(range_leases.c.range_id == range_id)).first() is not None:
        return "a worker task holds the range's lease"
    if db.execute(_in_flight().where(range_operations.c.range_id == range_id)).first() is not None:
        return "an operation of the range is in flight"
    if db.execute(sa.select(ranges.c.id).where(ranges.c.id == range_id, ranges.c.updated_at > since)).first():
        return "the range changed while it was read"
    s = range_snapshots
    if db.execute(sa.select(s.c.id).where(s.c.range_id == range_id, sa.or_(
            s.c.snapshot_state.in_(BUSY_SNAPSHOT_STATES), s.c.updated_at > since))).first() is not None:
        return "a snapshot of the range was being taken, restored or deleted while it was read"
    return None


def snapshot_rows(db, range_ids: list[str]) -> dict[str, list[dict]]:
    """{range id: [{"state", "data", "backend_name"}]} of the ranges' snapshot rows. The
    backend name is what the snapshot task names it (tasks._backend_snapshot_name)."""
    out: dict[str, list[dict]] = {str(r): [] for r in range_ids}
    if not range_ids:
        return out
    rows = db.execute(sa.select(range_snapshots.c.id, range_snapshots.c.range_id, range_snapshots.c.snapshot_state,
                                range_snapshots.c.snapshot_data)
                      .where(range_snapshots.c.range_id.in_(list(range_ids)))).fetchall()
    for snap_id, range_id, state, raw in rows:
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            data = {}
        backend_name = "tn" + "".join(ch for ch in str(snap_id) if ch.isalnum())[:38]
        out.setdefault(str(range_id), []).append({"state": state, "data": data if isinstance(data, dict) else {},
                                                  "backend_name": backend_name})
    return out


def append_entry(db, range_id: str, section: str, status: str, summary: str, detail: dict,
                 source: str = "worker.s5") -> bool:
    """Append to the range's newest certification record; False when it has none, or when
    the section's latest entry already says exactly this."""
    recs = range_certification_records
    ents = range_certification_entries
    rec = db.execute(sa.select(recs.c.id, recs.c.tenant_id).where(recs.c.range_id == range_id)
                     .order_by(recs.c.generation.desc()).limit(1)).first()
    if rec is None:
        return False
    db.execute(sa.select(recs.c.id).where(recs.c.id == rec[0]).with_for_update())  # one appender at a time
    detail = _plain(detail)
    top = db.execute(sa.select(ents.c.status, ents.c.summary, ents.c.detail)
                     .where(ents.c.record_id == rec[0], ents.c.section == section)
                     .order_by(ents.c.seq.desc()).limit(1)).first()
    if top is not None and (top[0], top[1], _plain(top[2])) == (status, summary[:500], detail):
        return False
    last = db.execute(sa.select(sa.func.max(ents.c.seq)).where(ents.c.record_id == rec[0])).scalar() or 0
    db.execute(sa.insert(ents).values(
        id=uuid.uuid4(), record_id=rec[0], tenant_id=rec[1], seq=last + 1, section=section, status=status,
        summary=summary[:500], detail=detail, source=source[:64], actor_id=None, actor="system", reason=None,
        corrects_seq=None, recorded_at=_now()))
    return True
