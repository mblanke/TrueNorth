"""Maintenance windows, re-checked at a range task's entry (ADR 0013 §7, range certification S7).

The API refuses a provision, a restore (a reset) or a person's normal destroy while a
maintenance window covers the range (app/maintenance_windows). A task accepted just
before a window opened, or sent late, can still reach a worker inside it. ``at_entry``
runs in ``fencing.fenced`` once the task holds the range's lease and before it touches
the hypervisor. A window never leaves a range ``failed``: the work is held and runs after
the window instead.

* ``provision`` is held inside a window. A recorded operation (the ranges router, the
  scheduler, exercises) goes back to ``pending`` with its range still ``provisioning``;
  the API's re-send loop leaves it there until the window ends (app/range_ops). A lab's
  build (no operation row) puts its range back to ``created``, and the lab session sends
  it again once the window is over (app/lab_sessions).
* ``restore`` is refused inside a window: the snapshot goes back to ``ready``; the range is
  untouched.
* ``destroy`` runs only when it is identified as one that must: its operation carries a
  bypass (an emergency, a booking that ended, failure cleanup, the canary), its operation
  was accepted before the window began, or the range is a Student lab's (its end, expiry,
  failed build, or a reset accepted before the window; lab sessions record why in
  ``end_reason``). Anything else is held: a person's destroy goes back to ``pending`` and is
  sent after the window; a destroy nothing identifies leaves the range ``destroying``, with
  the reason, untouched. If the windows cannot be read, a teardown still runs.

Reads go through the generated table mirror (tables.py); no raw SQL (ADR 0003). Windows are
read for the range's tenant and the platform only, in effect now, with no limit: another
tenant's windows can neither hide nor stand in for this one's. ``applies`` and
``template_tags`` are the API's (app/maintenance_windows/policy.py), copied because the
worker cannot import the API; tests/contracts/test_maintenance_window_policy.py keeps them
equal. YAML is parsed as the API's safe_yaml.load does it: no aliases, size-capped.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa
import yaml

from .tables import lab_sessions, maintenance_windows, range_operations, ranges, templates

logger = logging.getLogger("worker.maintenance_windows")

IN_FLIGHT = ("pending", "dispatched")  # app/range_ops/models.py
GATED = {"provision": "provisioning", "destroy": "destroying", "restore": None}
BYPASS_KINDS = ("emergency", "booking_ended", "failure_cleanup", "canary")  # app/maintenance_windows/policy.py
_WHAT = {"provision": "Provisioning", "destroy": "Destroying", "restore": "Restoring a snapshot"}
MAX_YAML_BYTES = 1024 * 1024  # app/safe_yaml.py


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader without anchors/aliases (app/safe_yaml.py's NoAliasLoader)."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted")
        return super().compose_node(parent, index)


def _safe_load(text: str):
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise yaml.YAMLError(f"document is larger than {MAX_YAML_BYTES} bytes")
    return yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass


# ── copied from app/maintenance_windows/policy.py (contract-tested) ─────────
def template_tags(template_yaml: str | None) -> set[str]:
    """The tags a range template declares at its top level, lowercased."""
    if not template_yaml:
        return set()
    try:
        doc = _safe_load(template_yaml)
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
    rng = db.execute(sa.select(ranges.c.tenant_id, ranges.c.template_id).where(ranges.c.id == range_id)).first()
    if rng is None:
        return None
    tenant_filter = maintenance_windows.c.tenant_id.is_(None)
    if rng[0] is not None:
        tenant_filter = sa.or_(tenant_filter, maintenance_windows.c.tenant_id == rng[0])
    rows = (
        db.execute(
            sa.select(maintenance_windows)
            .where(tenant_filter, maintenance_windows.c.starts_at <= now, maintenance_windows.c.ends_at > now)
            .order_by(maintenance_windows.c.ends_at.desc())
        )
        .mappings()
        .all()
    )
    tags: set[str] | None = None
    for w in rows:
        start, end = _utc(w["starts_at"]), _utc(w["ends_at"])
        if start is None or end is None or not start <= now < end:  # [starts_at, ends_at)
            continue
        if w["scope"] == "tags" and tags is None:
            row = db.execute(sa.select(templates.c.yaml).where(templates.c.id == rng[1])).first()
            tags = template_tags(row[0] if row else None)
        if applies(
            window_tenant=w["tenant_id"], scope=w["scope"], range_ids=w["range_ids"], tags=w["tags"],
            range_id=range_id, range_tenant=rng[0], range_tags=tags or set(),
        ):
            return dict(w)
    return None


def _operation(db, range_id: str, action: str):
    """The range's newest operation, when it is this action and still in flight."""
    op = db.execute(
        sa.select(
            range_operations.c.id, range_operations.c.action, range_operations.c.status,
            range_operations.c.window_bypass, range_operations.c.created_at,
        )
        .where(range_operations.c.range_id == range_id)
        .order_by(range_operations.c.generation.desc())
        .limit(1)
    ).first()
    return op if op is not None and op[1] == action and op[2] in IN_FLIGHT else None


def _lab_session(db, range_id: str):
    return db.execute(
        sa.select(lab_sessions.c.id, lab_sessions.c.state, lab_sessions.c.end_reason).where(
            lab_sessions.c.range_id == range_id
        )
    ).first()


def message(w: dict, action: str) -> str:
    end = _utc(w["ends_at"])
    return (
        f"{_WHAT.get(action, action)} is held by the worker: a maintenance window is in effect until "
        f"{end.isoformat() if end else w['ends_at']}: {w['reason']}. Nothing was changed on the hypervisor; "
        "it runs after the window."
    )


def decide(db, action: str, range_id: str, now: datetime | None = None) -> tuple[str | None, object]:
    """(why this task may not run now, or None; the in-flight operation, if any)."""
    if action not in GATED:
        return None, None
    op = _operation(db, range_id, action) if action != "restore" else None
    w = covering(db, range_id, now)
    if w is None:
        return None, op
    if action == "destroy":
        if op is not None and op[3] in BYPASS_KINDS:
            logger.info("[destroy] range %s: %s teardown runs inside a maintenance window", range_id, op[3])
            return None, op
        accepted, start = _utc(op[4]) if op is not None else None, _utc(w["starts_at"])
        if accepted is not None and start is not None and accepted < start:
            return None, op  # asked for before the window began
        if op is None and (lab := _lab_session(db, range_id)) is not None:
            logger.info("[destroy] range %s: lab session %s cleanup (%s, %s) runs inside a maintenance window",
                        range_id, lab[0], lab[1], lab[2] or "-")
            return None, op
    return message(w, action), op


def _hold_operation(db, op, why: str) -> None:
    """Back to ``pending`` (the outbox): the API sends it again once the window is over."""
    db.execute(
        sa.update(range_operations)
        .where(range_operations.c.id == op[0], range_operations.c.status == "dispatched")
        .values(status="pending", dispatched_at=None, error={"code": "maintenance_window", "message": why[:500]})
    )


def at_entry(session_factory, action: str, range_id: str, *args) -> dict | None:
    """Called by ``fencing.fenced`` with the lease held. None: run the task. Otherwise the
    task's result: the work is held for after the window, and nothing is marked failed."""
    if action not in GATED:
        return None
    try:
        with session_factory() as db:
            why, op = decide(db, action, range_id)
    except Exception as exc:  # noqa: BLE001
        if action == "destroy":
            logger.error("[destroy] range %s: maintenance windows unreadable (%s); the teardown runs", range_id, exc)
            return None
        why, op = f"{_WHAT[action]} is held by the worker: maintenance windows could not be read ({exc}).", None
        try:
            with session_factory() as db:
                op = _operation(db, range_id, action)
        except Exception:  # noqa: BLE001
            op = None
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
            if op is not None:
                # The range stays provisioning / destroying; the operation's error says why
                # (not the range's, which a later success would not clear).
                _hold_operation(db, op, why)
            elif action == "provision":  # a lab's build: back to created; its session sends it again
                guarded_range_update(db, range_id, "created", error=why, only_from=("provisioning",))
            else:  # a destroy nothing identifies: not run, not failed
                guarded_range_update(db, range_id, "destroying", error=why, only_from=("destroying",))
        notify_api("range", {"id": range_id, "held": action, "error": why})
    return {"status": "held", "reason": "maintenance_window", "range_id": range_id, "detail": why}
