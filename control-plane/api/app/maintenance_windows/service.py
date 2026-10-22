"""Maintenance windows: which are in effect, what they refuse, and their administration
(ADR 0013 §7, range certification S7).

Other sections reach windows only through this module:

* ``refuse_inside(db, rng, action)``: 409 with the window's reason and end time. Called by
  ``range_ops.accept`` for a provision or a normal destroy (every caller: the ranges
  router, batch provisioning, exercises, the scheduler), by the snapshot restore, and by
  a lab session's reset.
* ``blocking_new_range(db, tenant_id)``: a window that holds back a range not created yet
  (a lab waiting in its queue).
* ``booking_warnings(...)``: what the scheduler tells an Instructor whose booking overlaps
  a window; ``provision_blocked(...)``: whether the clock may build a booked range now.

Never refused (``policy.BYPASS_KINDS``): automated cleanup and an authorised emergency
teardown. The worker re-checks at a range task's entry (worker/maintenance_windows.py).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Range, Template
from ..rbac import Permission, is_platform_admin, user_has_permission
from ..tenancy import tenant_uuid
from . import policy
from .models import MaintenanceWindow
from .schemas import MaintenanceWindowIn, MaintenanceWindowOut, WindowScope

logger = logging.getLogger("truenorth.api.maintenance_windows")

_WHAT = {
    "provision": "Provisioning",
    "destroy": "Destroying",
    "restore": "Restoring a snapshot",
    "reset": "Resetting",
}


def _now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime) -> datetime:
    """SQLite hands timezone columns back naive; they were written in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# Windows not yet ended that one tenant (or the platform) may hold at once: bounds every
# lookup above, which reads all of a tenant's and the platform's live windows.
MAX_LIVE_WINDOWS = 50

_TICK = timedelta(microseconds=1)  # [at, at + 1 µs): "in effect at this instant" as an overlap


def _in_effect(w: MaintenanceWindow, at: datetime) -> bool:
    return utc(w.starts_at) <= at < utc(w.ends_at)  # [starts_at, ends_at)


def label(w: MaintenanceWindow) -> str:
    return f"{utc(w.starts_at):%Y-%m-%d %H:%M} to {utc(w.ends_at):%Y-%m-%d %H:%M} UTC"


# ── Which windows cover a range ─────────────────────────────────────────────
def _candidates(db: Session, tenant_id: uuid.UUID | None, start: datetime, end: datetime) -> list[MaintenanceWindow]:
    """Windows of the tenant, and platform-wide ones, overlapping [start, end), every one of
    them. No LIMIT before the scope is matched: a tenant creating many windows must never
    push a platform-wide window (or any other) out of the set. Bounded instead by the cap on
    windows a tenant may hold (``MAX_LIVE_WINDOWS``), and to the tenant plus platform-wide."""
    tenant_filter = MaintenanceWindow.tenant_id.is_(None)
    if tenant_id is not None:
        tenant_filter = or_(tenant_filter, MaintenanceWindow.tenant_id == tenant_id)
    return (
        db.query(MaintenanceWindow)
        .filter(tenant_filter, MaintenanceWindow.starts_at < end, MaintenanceWindow.ends_at > start)
        .order_by(MaintenanceWindow.ends_at.desc())
        .all()
    )


def _range_tags_loader(db: Session, template_id: uuid.UUID | None) -> Callable[[], set[str]]:
    """The range template's tags, read once and only if a ``tags`` window needs them."""
    cache: list[set[str]] = []

    def load() -> set[str]:
        if not cache:
            # tenant-safe: the template of a range (or booking) the caller already resolved in its tenant.
            tmpl = db.get(Template, template_id) if template_id else None
            cache.append(policy.template_tags(tmpl.yaml if tmpl else None))
        return cache[0]

    return load


def _covering(
    windows: list[MaintenanceWindow],
    *,
    tenant_id: uuid.UUID | None,
    range_id: uuid.UUID | None,
    tags: Callable[[], set[str]],
) -> list[MaintenanceWindow]:
    out = []
    for w in windows:
        range_tags = tags() if w.scope == WindowScope.tags.value else set()
        if policy.applies(
            window_tenant=w.tenant_id, scope=w.scope, range_ids=w.range_ids, tags=w.tags,
            range_id=range_id, range_tenant=tenant_id, range_tags=range_tags,
        ):
            out.append(w)
    return out


def covering_range(db: Session, rng: Range, at: datetime | None = None) -> list[MaintenanceWindow]:
    """Windows in effect at ``at`` (now) that cover ``rng``, the latest-ending first."""
    at = at or _now()
    windows = [w for w in _candidates(db, rng.tenant_id, at, at + _TICK) if _in_effect(w, at)]
    return _covering(windows, tenant_id=rng.tenant_id, range_id=rng.id, tags=_range_tags_loader(db, rng.template_id))


def held_range_ids(db: Session, ranges: list[Range], at: datetime | None = None) -> set:
    """Of ``ranges``, the ids a window in effect at ``at`` (now) covers. One query for the
    windows of all their tenants and the platform, one for the templates a ``tags`` window
    needs, however many ranges."""
    at = at or _now()
    if not ranges:
        return set()
    tenants = {r.tenant_id for r in ranges if r.tenant_id is not None}
    live = [
        w for w in db.query(MaintenanceWindow).filter(
            or_(MaintenanceWindow.tenant_id.is_(None), MaintenanceWindow.tenant_id.in_(tenants)),
            MaintenanceWindow.starts_at < at + _TICK, MaintenanceWindow.ends_at > at,
        ).all()
        if _in_effect(w, at)
    ]
    if not live:
        return set()
    tags: dict = {}
    if any(w.scope == WindowScope.tags.value for w in live):
        # tenant-safe: the templates of ranges the caller already resolved.
        rows = db.query(Template.id, Template.yaml).filter(Template.id.in_({r.template_id for r in ranges})).all()
        tags = {tid: policy.template_tags(y) for tid, y in rows}
    return {
        r.id for r in ranges
        if _covering(live, tenant_id=r.tenant_id, range_id=r.id, tags=lambda r=r: tags.get(r.template_id, set()))
    }


def blocking_new_range(db: Session, tenant_id: uuid.UUID, at: datetime | None = None) -> MaintenanceWindow | None:
    """An ``all`` window in effect for the tenant: it holds back a range not created yet."""
    at = at or _now()
    windows = [w for w in _candidates(db, tenant_id, at, at + _TICK) if _in_effect(w, at)]
    hit = _covering(windows, tenant_id=tenant_id, range_id=None, tags=set)
    return hit[0] if hit else None


def refusal_message(w: MaintenanceWindow, action: str) -> str:
    return (
        f"{_WHAT.get(action, action.capitalize())} is refused during a maintenance window until "
        f"{utc(w.ends_at).isoformat()} ({label(w)}): {w.reason}. Automated cleanup still runs; an "
        "administrator can tear a range down in an emergency (range:admin, with a reason)."
    )


def refuse_inside(db: Session, rng: Range, action: str) -> None:
    """409, with the window's reason and end time, when a window covers ``rng`` now.
    ``Retry-After`` says when it ends."""
    hit = covering_range(db, rng)
    if not hit:
        return
    w = hit[0]  # the latest-ending: the range is free once that one is over
    wait = max(int((utc(w.ends_at) - _now()).total_seconds()), 1)
    raise HTTPException(409, refusal_message(w, action), headers={"Retry-After": str(wait)})


def refusal_for_new_range(db: Session, tenant_id: uuid.UUID, action: str = "provision") -> str | None:
    w = blocking_new_range(db, tenant_id)
    return refusal_message(w, action) if w else None


# ── For the scheduler ───────────────────────────────────────────────────────
def booking_warnings(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    range_id: uuid.UUID | None,
    template_id: uuid.UUID | None,
    build_from: datetime,
    end: datetime,
) -> list[str]:
    """Windows overlapping a booking's build-to-end span [build_from, end), as warnings. The
    booking is still made; the clock does not build its range while a window covers it."""
    build_from, end = utc(build_from), utc(end)
    if range_id is not None:
        # tenant-safe: a booking's range, resolved in the caller's tenant (scheduler.service.resolve_range).
        rng = db.get(Range, range_id)
        template_id = rng.template_id if rng is not None else template_id
    hit = _covering(
        _candidates(db, tenant_id, build_from, end),
        tenant_id=tenant_id, range_id=range_id, tags=_range_tags_loader(db, template_id),
    )
    return [
        f"Overlaps maintenance window {label(w)} ({w.reason}): the range is not built while the window is in "
        "effect; it is built on the scheduler's first tick after the window ends, if the session has not ended"
        for w in sorted(hit, key=lambda w: utc(w.starts_at))
    ]


def provision_blocked(
    db: Session, *, tenant_id: uuid.UUID, range_id: uuid.UUID | None, at: datetime | None = None
) -> MaintenanceWindow | None:
    """The window that keeps the scheduler clock from building a booked range at ``at``
    (now), if any."""
    # tenant-safe: a booking's range, checked against its tenant when booked.
    rng = db.get(Range, range_id) if range_id is not None else None
    if rng is None:
        return blocking_new_range(db, tenant_id, at)
    hit = covering_range(db, rng, at)
    return hit[0] if hit else None


# ── Administration ──────────────────────────────────────────────────────────
def can_edit(db: Session, user: CurrentUser, w: MaintenanceWindow) -> bool:
    if not user_has_permission(user, Permission.RANGE_ADMIN):
        return False
    if w.tenant_id is None:
        return is_platform_admin(user, db)
    return w.tenant_id == tenant_uuid(user)


def visible(db: Session, user: CurrentUser):
    """The caller's tenant's windows and the platform-wide ones."""
    return db.query(MaintenanceWindow).filter(
        or_(MaintenanceWindow.tenant_id == tenant_uuid(user), MaintenanceWindow.tenant_id.is_(None))
    )


def get_visible(db: Session, user: CurrentUser, window_id: uuid.UUID) -> MaintenanceWindow:
    w = visible(db, user).filter(MaintenanceWindow.id == window_id).first()
    if w is None:
        raise HTTPException(404, "Maintenance window not found")
    return w


def get_editable(db: Session, user: CurrentUser, window_id: uuid.UUID) -> MaintenanceWindow:
    """404 for another tenant's window; 403 for a platform-wide one, unless the caller is a
    platform administrator."""
    w = get_visible(db, user, window_id)
    if not can_edit(db, user, w):
        raise HTTPException(403, "Only a platform administrator can change a platform-wide maintenance window")
    return w


def _resolve_ranges(db: Session, user: CurrentUser, ids: list[uuid.UUID], platform_wide: bool) -> list[str]:
    """A tenant's window names only its own ranges (404 otherwise, as for any range). A
    platform-wide window may name any tenant's."""
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    q = db.query(Range.id).filter(Range.id.in_(wanted))
    if not platform_wide:
        q = q.filter(Range.tenant_id == tenant_uuid(user))
    found = {r[0] for r in q.all()}
    if missing := [i for i in wanted if i not in found]:
        raise HTTPException(404, f"Range not found: {missing[0]}")
    return [str(i) for i in wanted]


def _apply(db: Session, user: CurrentUser, w: MaintenanceWindow, body: MaintenanceWindowIn) -> None:
    if body.ends_at <= _now():
        raise HTTPException(422, "ends_at is in the past; a maintenance window must end in the future")
    w.reason = body.reason.strip()
    w.starts_at, w.ends_at = body.starts_at, body.ends_at
    w.scope = body.scope.value
    w.range_ids = _resolve_ranges(db, user, body.range_ids, w.tenant_id is None)
    w.tags = sorted({t.strip().lower() for t in body.tags if t.strip()})
    w.updated_by = _user_uuid(user)


def create(db: Session, user: CurrentUser, body: MaintenanceWindowIn) -> MaintenanceWindow:
    """range:admin; a platform-wide window also needs a platform administrator. Does not commit."""
    if body.platform_wide and not is_platform_admin(user, db):
        raise HTTPException(403, "Only a platform administrator can create a platform-wide maintenance window")
    _check_cap(db, None if body.platform_wide else tenant_uuid(user))
    w = MaintenanceWindow(
        id=uuid.uuid4(), tenant_id=None if body.platform_wide else tenant_uuid(user), created_by=_user_uuid(user)
    )
    _apply(db, user, w, body)
    db.add(w)
    db.flush()
    audit(db, user, "create", w, describe(w))
    return w


def update(db: Session, user: CurrentUser, w: MaintenanceWindow, body: MaintenanceWindowIn) -> MaintenanceWindow:
    """Who the window applies to (its tenant, or platform-wide) is fixed once created. Does not commit."""
    if body.platform_wide != (w.tenant_id is None):
        raise HTTPException(422, "platform_wide cannot be changed; delete the window and create another")
    if utc(w.ends_at) <= _now():  # an ended window brought back counts again
        _check_cap(db, w.tenant_id)
    before = describe(w)
    _apply(db, user, w, body)
    db.flush()
    audit(db, user, "update", w, f"{before} -> {describe(w)}")
    return w


def _check_cap(db: Session, owner: uuid.UUID | None) -> None:
    """422 once ``owner`` (a tenant, or None: the platform) holds ``MAX_LIVE_WINDOWS`` windows
    that have not ended. The count and the write that follows are serialised per owner
    (a transaction-scoped advisory lock on PostgreSQL; SQLite serialises writers anyway)."""
    if db.get_bind().dialect.name == "postgresql":
        key = f"truenorth.maintenance_windows.{owner or 'platform'}"
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
    live = db.query(MaintenanceWindow.id).filter(
        MaintenanceWindow.tenant_id.is_(None) if owner is None else MaintenanceWindow.tenant_id == owner,
        MaintenanceWindow.ends_at > _now(),
    ).count()
    if live >= MAX_LIVE_WINDOWS:
        raise HTTPException(
            422, f"At most {MAX_LIVE_WINDOWS} maintenance windows may be in effect or upcoming at once; "
            "delete or shorten one first"
        )


def delete(db: Session, user: CurrentUser, w: MaintenanceWindow) -> None:
    """Does not commit. The audit entry keeps what the window was."""
    audit(db, user, "delete", w, describe(w))
    db.delete(w)
    db.flush()


def describe(w: MaintenanceWindow) -> str:
    where = "platform-wide" if w.tenant_id is None else "tenant"
    what = {"ranges": f" ranges={','.join(w.range_ids or [])}", "tags": f" tags={','.join(w.tags or [])}"}
    return f"{where} {label(w)} scope={w.scope}{what.get(w.scope, '')} reason={w.reason!r}"


def to_out(db: Session, user: CurrentUser, w: MaintenanceWindow, mine: set[str] | None = None) -> MaintenanceWindowOut:
    """``mine``: the caller's tenant's range ids among a platform-wide window's (another
    tenant's range ids are not shown to a tenant that does not own them)."""
    ids = list(w.range_ids or [])
    if w.tenant_id is None and not is_platform_admin(user, db):
        ids = [i for i in ids if mine is not None and i in mine]
    now = _now()
    return MaintenanceWindowOut(
        id=w.id, tenant_id=w.tenant_id, platform_wide=w.tenant_id is None, reason=w.reason,
        starts_at=utc(w.starts_at), ends_at=utc(w.ends_at), scope=WindowScope(w.scope),
        range_ids=[uuid.UUID(i) for i in ids], tags=list(w.tags or []),
        active=_in_effect(w, now), can_edit=can_edit(db, user, w),
        created_by=w.created_by, created_at=utc(w.created_at) if w.created_at else None,
        updated_at=utc(w.updated_at) if w.updated_at else None,
    )


def own_range_ids(db: Session, user: CurrentUser, windows: list[MaintenanceWindow]) -> set[str]:
    """Of the range ids platform-wide windows list, those that are the caller's tenant's."""
    ids = {i for w in windows if w.tenant_id is None for i in (w.range_ids or [])}
    if not ids:
        return set()
    rows = db.query(Range.id).filter(Range.id.in_([uuid.UUID(i) for i in ids]), Range.tenant_id == tenant_uuid(user))
    return {str(r[0]) for r in rows.all()}


def audit(db: Session, user: CurrentUser, action: str, w: MaintenanceWindow, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=_user_uuid(user),
            tenant_id=tenant_uuid(user),
            action=action,
            resource_type="maintenance_window",
            resource_id=str(w.id),
            detail=detail[:2000],
        )
    )


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None
