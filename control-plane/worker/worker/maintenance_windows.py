"""Maintenance windows, re-checked at a range task's entry (ADR 0013 §7, range certification S7).

The API refuses a provision, a restore (a reset) or a person's normal destroy while a
maintenance window covers the range (app/maintenance_windows). A task accepted just
before a window opened, or sent late, can still reach a worker inside it. ``at_entry``
runs in ``fencing.fenced`` once the task holds the range's lease and before it touches
the hypervisor:

* ``provision``: refused inside a window. The range goes to ``failed`` with the reason and
  nothing is built (a lab session then tears its range down, which always runs).
* ``restore``: refused inside a window. The snapshot goes back to ``ready``; the range is
  untouched.
* ``destroy``: refused only when the range's newest operation is a person's normal destroy
  still in flight (``range_operations.window_bypass`` null). Every other destroy runs:
  an emergency teardown, a booking that ended, a lab session's end, expiry or failed build
  (they send no operation row), and anything this check cannot identify. Cleanup is never
  blocked by a window, nor by a failure to read one.

Reads go through the generated table mirror (tables.py); no raw SQL (ADR 0003). ``applies``
and ``template_tags`` are the API's (app/maintenance_windows/policy.py), copied because the
worker cannot import the API; tests/contracts/test_maintenance_window_policy.py keeps them equal.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from .tables import maintenance_windows, range_operations, ranges, templates

logger = logging.getLogger("worker.maintenance_windows")

IN_FLIGHT = ("pending", "dispatched")  # app/range_ops/models.py
GATED = {"provision": "provisioning", "destroy": "destroying", "restore": None}
_WHAT = {"provision": "Provisioning", "destroy": "Destroying", "restore": "Restoring a snapshot"}


# ── copied from app/maintenance_windows/policy.py (contract-tested) ─────────
def template_tags(template_yaml: str | None) -> set[str]:
    """The tags a range template declares at its top level, lowercased."""
    if not template_yaml:
        return set()
    try:
        import yaml

        doc = yaml.safe_load(template_yaml)
    except Exception:  # noqa: BLE001 — a template that does not parse declares no tags
        return set()
    raw = doc.get("tags") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().lower() for t in raw if isinstance(t, str | int) and str(t).strip()}


def applies(
    *,
    window_tenant,
    scope: str,
    range_ids: Iterable[str] | None,
    tags: Iterable[str] | None,
    range_id,
    range_tenant,
    range_tags: Iterable[str] | None,
) -> bool:
    """Whether a window covers a range."""
    if window_tenant is not None and str(window_tenant) != str(range_tenant):
        return False
    if scope == "all":
        return True
    if range_id is None:
        return False
    if scope == "ranges":
        return str(range_id) in {str(r) for r in (range_ids or [])}
    if scope == "tags":
        wanted = {str(t).strip().lower() for t in (tags or [])}
        return bool(wanted & {str(t).lower() for t in (range_tags or [])})
    return False


# ── the check ──────────────────────────────────────────────────────────────
def _utc(value) -> datetime | None:
    if isinstance(value, str):  # SQLite without type processing
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def covering(db, range_id: str, now: datetime | None = None) -> dict | None:
    """The window in effect now that covers the range (the latest-ending), or None."""
    now = now or datetime.now(UTC)
    rows = (
        db.execute(
            sa.select(maintenance_windows)
            .where(maintenance_windows.c.ends_at > now)
            .order_by(maintenance_windows.c.ends_at.desc())
            .limit(200)
        )
        .mappings()
        .all()
    )
    live = []
    for w in rows:
        start, end = _utc(w["starts_at"]), _utc(w["ends_at"])
        if start is not None and end is not None and start <= now < end:  # [starts_at, ends_at)
            live.append(w)
    if not live:
        return None
    rng = db.execute(sa.select(ranges.c.tenant_id, ranges.c.template_id).where(ranges.c.id == range_id)).first()
    if rng is None:
        return None
    tags: set[str] | None = None
    for w in live:
        if w["scope"] == "tags" and tags is None:
            row = db.execute(sa.select(templates.c.yaml).where(templates.c.id == rng[1])).first()
            tags = template_tags(row[0] if row else None)
        if applies(
            window_tenant=w["tenant_id"], scope=w["scope"], range_ids=w["range_ids"], tags=w["tags"],
            range_id=range_id, range_tenant=rng[0], range_tags=tags or set(),
        ):
            return dict(w)
    return None


def _normal_destroy_in_flight(db, range_id: str) -> bool:
    """The range's newest operation is a person's destroy, still in flight, with no bypass."""
    op = db.execute(
        sa.select(range_operations.c.action, range_operations.c.status, range_operations.c.window_bypass)
        .where(range_operations.c.range_id == range_id)
        .order_by(range_operations.c.generation.desc())
        .limit(1)
    ).first()
    return bool(op) and op[0] == "destroy" and op[1] in IN_FLIGHT and not op[2]


def message(w: dict, action: str) -> str:
    end = _utc(w["ends_at"])
    return (
        f"{_WHAT.get(action, action)} was refused by the worker: a maintenance window is in effect until "
        f"{end.isoformat() if end else w['ends_at']}: {w['reason']}. Nothing was changed on the hypervisor."
    )


def refusal(db, action: str, range_id: str, now: datetime | None = None) -> str | None:
    """Why this task may not run now, or None."""
    if action not in GATED:
        return None
    if action == "destroy" and not _normal_destroy_in_flight(db, range_id):
        return None  # cleanup and emergencies always run
    w = covering(db, range_id, now)
    return message(w, action) if w else None


def at_entry(session_factory, action: str, range_id: str, *args) -> dict | None:
    """Called by ``fencing.fenced`` with the lease held. None: run the task. Otherwise the
    task's result: the refusal has been recorded (range ``failed``, or the snapshot back
    to ``ready``)."""
    if action not in GATED:
        return None
    try:
        with session_factory() as db:
            why = refusal(db, action, range_id)
    except Exception as exc:  # noqa: BLE001
        if action == "destroy":
            logger.error("[destroy] range %s: maintenance windows unreadable (%s); the teardown runs", range_id, exc)
            return None
        why = f"{_WHAT[action]} was refused by the worker: maintenance windows could not be read ({exc})."
    if not why:
        return None
    logger.warning("[%s] range %s: %s", action, range_id, why)
    from .fencing import guarded_range_update, snapshot_back_to_ready  # fencing imports this module
    from .task_plumbing import notify_api

    if action == "restore":
        snapshot_back_to_ready(range_id, *args)
        notify_api("range", {"id": range_id, "restore_failed": args[0] if args else None, "error": why})
    else:
        with session_factory() as db:
            guarded_range_update(db, range_id, "failed", error=why, only_from=(GATED[action],))
        notify_api("range", {"id": range_id, "state": "failed", "error": why})
    return {"status": "refused", "reason": "maintenance_window", "range_id": range_id, "detail": why}
