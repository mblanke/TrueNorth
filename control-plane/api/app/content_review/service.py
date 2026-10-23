"""The content clock's service: who may see and change what, and the writes (ADR 0014
decisions 22-24, issue #286). The rules are in ``staleness`` (pure).

Access (``rbac.py``):

* ``content_review:read`` (admins, instructors, range ops) reads the tenant's content and the
  shared catalogue (``tenant_id`` null). Another tenant's content is not found (404).
* ``content_review:review_own`` (instructors) marks reviewed only the tenant's content they
  own or were assigned to review, and assigns a reviewer only to content they own (ADR 0014:
  reviewers are the content's owner plus admins). Never shared content.
* ``content_review:admin`` (admins) does everything to the tenant's content. Shared content is
  changed only by a platform administrator.

Mark reviewed, assign and retire (and every admin change) write an ``AuditLog`` row.
Nothing here commits; the router does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, User, UserRole
from ..rbac import Permission, is_platform_admin, user_has_permission
from ..tenancy import tenant_uuid
from . import staleness
from .models import ITEM_TYPES, ContentReviewCycle, ContentReviewEdge, ContentReviewItem, ContentReviewTheme
from .schemas import (
    ContentClockOut,
    ContentItemBrief,
    ContentItemDetailOut,
    ContentItemOut,
    ContentItemUpdateIn,
    CycleIn,
    CycleOut,
    StaleReasonOut,
    ThemeIn,
    ThemeOut,
)

REVIEWER_ROLES = (UserRole.admin, UserRole.instructor)


def now() -> datetime:
    return datetime.now(UTC)


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


# ── visibility and permission ────────────────────────────────────────────────
def visible(db: Session, user: CurrentUser):
    """The caller's tenant's content and the shared catalogue, never content that is gone."""
    return db.query(ContentReviewItem).filter(
        or_(ContentReviewItem.tenant_id == tenant_uuid(user), ContentReviewItem.tenant_id.is_(None)),
        ContentReviewItem.missing.is_(False),
    )


def get_visible(db: Session, user: CurrentUser, item_id: uuid.UUID, *, lock: bool = False) -> ContentReviewItem:
    q = visible(db, user).filter(ContentReviewItem.id == item_id)
    if lock:
        q = q.with_for_update()
    row = q.first()
    if row is None:
        raise HTTPException(404, "Content item not found")
    return row


def can_admin(db: Session, user: CurrentUser, row: ContentReviewItem) -> bool:
    if not user_has_permission(user, Permission.CONTENT_REVIEW_ADMIN):
        return False
    if row.tenant_id is None:
        return is_platform_admin(user, db)
    return row.tenant_id == tenant_uuid(user)


def owns(user: CurrentUser, row: ContentReviewItem) -> bool:
    """The caller owns the item, or is its assigned reviewer."""
    me = _uid(user)
    return me is not None and me in (row.owner_id, row.reviewer_id)


def can_review(db: Session, user: CurrentUser, row: ContentReviewItem) -> bool:
    if can_admin(db, user, row):
        return True
    return (user_has_permission(user, Permission.CONTENT_REVIEW_OWN) and row.tenant_id is not None
            and row.tenant_id == tenant_uuid(user) and owns(user, row))


def can_assign(db: Session, user: CurrentUser, row: ContentReviewItem) -> bool:
    """Admins, or the instructor who owns it (not merely its assigned reviewer)."""
    if can_admin(db, user, row):
        return True
    me = _uid(user)
    return (user_has_permission(user, Permission.CONTENT_REVIEW_OWN) and row.tenant_id is not None
            and row.tenant_id == tenant_uuid(user) and me is not None and row.owner_id == me)


def require_review(db: Session, user: CurrentUser, row: ContentReviewItem) -> None:
    if not can_review(db, user, row):
        if row.tenant_id is None:
            raise HTTPException(403, "Shared catalogue content is changed only by a platform administrator")
        raise HTTPException(403, "Only an admin, or the content's owner or assigned reviewer, can do this")


def require_admin(db: Session, user: CurrentUser, row: ContentReviewItem) -> None:
    if not can_admin(db, user, row):
        if row.tenant_id is None:
            raise HTTPException(403, "Shared catalogue content is changed only by a platform administrator")
        raise HTTPException(403, "Only an admin can do this")


# ── cycles ───────────────────────────────────────────────────────────────────
class Cycles:
    """The cycle per (tenant, content type): the tenant's, else the platform's, else 90.
    Shared content (tenant null) uses the platform's."""

    def __init__(self, db: Session):
        self._rows = {(r.tenant_id, r.item_type): r.days for r in db.query(ContentReviewCycle).all()}

    def get(self, tenant_id: uuid.UUID | None, item_type: str) -> tuple[int, str]:
        if tenant_id is not None and (tenant_id, item_type) in self._rows:
            return self._rows[(tenant_id, item_type)], "tenant"
        if (None, item_type) in self._rows:
            return self._rows[(None, item_type)], "platform"
        return staleness.DEFAULT_CYCLE_DAYS, "default"

    def for_row(self, row: ContentReviewItem) -> int:
        return row.cycle_days or self.get(row.tenant_id, row.item_type)[0]


def refresh_clock(row: ContentReviewItem, cycle_days: int, at: datetime) -> bool:
    """Recompute the due date and the item's own overdue reason. Returns whether it changed."""
    due = staleness.due_at(baseline=_utc(row.created_at) or at, last_reviewed=_utc(row.last_reviewed_at),
                           cycle_days=cycle_days)
    key = str(row.id)
    graph = staleness.Graph({key: list(row.stale_reasons or [])}, {}, {})
    changed = graph.set_overdue(key, staleness.overdue_text(due, at), at=at)
    old_due = _utc(row.due_at)
    if changed:
        row.stale_reasons = [dict(r) for r in graph.reasons[key]]
        row.stale = bool(row.stale_reasons)
    if old_due != due:
        row.due_at = due
        changed = True
    if changed:
        row.revision = (row.revision or 0) + 1
    return changed


# ── the dependency graph ─────────────────────────────────────────────────────
def lock_query(db: Session, ids: Iterable[str]):
    """The review rows ``ids`` (not missing), locked FOR UPDATE in id order and re-read."""
    wanted = [uuid.UUID(str(i)) for i in ids]
    return (db.query(ContentReviewItem)
            .filter(ContentReviewItem.id.in_(wanted), ContentReviewItem.missing.is_(False))
            .order_by(ContentReviewItem.id).with_for_update().populate_existing())


def lock_rows(db: Session, ids: Iterable[str]) -> dict[str, ContentReviewItem]:
    return {str(r.id): r for r in lock_query(db, ids).all()}


def load_graph(db: Session, key: str) -> tuple[staleness.Graph, dict[str, ContentReviewItem]]:
    """``key`` and everything downstream of it, over every edge.

    The edges are read unlocked; only the rows a change at ``key`` can rewrite (``key`` and
    its dependents) are locked, FOR UPDATE in id order and re-read, so two reviews, or a review
    and a feed flag, never each write from their own snapshot. The worker's sync locks the rows
    it writes in the same order and keeps a row whose ``revision`` changed since it read it.
    Nothing of an unrelated item, or another tenant's, is read or locked."""
    db.flush()  # re-reading (populate_existing) would otherwise drop this session's own unflushed writes
    depends: dict[str, set[str]] = {}
    for item_id, dep_id in db.query(ContentReviewEdge.item_id, ContentReviewEdge.depends_on_id).all():
        depends.setdefault(str(item_id), set()).add(str(dep_id))
    used = staleness.used_by(depends)
    reach = {key} | {k for k, _ in staleness.Graph({}, used, {}).dependents(key)}
    rows = lock_rows(db, reach)
    names = {k: (ITEM_TYPES.get(r.item_type, r.item_type), r.title) for k, r in rows.items()}
    graph = staleness.Graph({k: list(r.stale_reasons or []) for k, r in rows.items()}, used, names,
                            retired={k for k, r in rows.items() if r.retired})
    return graph, rows


def _write_back(graph: staleness.Graph, rows: dict[str, ContentReviewItem], changed: Iterable[str]) -> None:
    for key in changed:
        row = rows.get(key)
        if row is None:
            continue
        row.stale_reasons = [dict(r) for r in graph.reasons.get(key, [])]
        row.stale = bool(row.stale_reasons)
        row.revision = (row.revision or 0) + 1


def flag_stale(db: Session, item: ContentReviewItem | uuid.UUID | str, reason: str, source: str, *,
               kind: str = staleness.KIND_RELEASE, at: datetime | None = None) -> set[str]:
    """Flag ``item`` stale because of ``source`` (a release or vulnerability feed entry, ADR
    0014 decision 25), and everything downstream of it. The hook for the feeds (issue
    #287); it does not commit. Returns the ids of the review items that changed."""
    key = str(item.id if isinstance(item, ContentReviewItem) else item)
    graph, rows = load_graph(db, key)
    if key not in rows:
        raise KeyError(f"no content review item {key}")
    changed = graph.flag_stale(key, reason, source, kind=kind, at=at or now())
    _write_back(graph, rows, changed)
    return changed


# ── the writes ───────────────────────────────────────────────────────────────
def audit(db: Session, user: CurrentUser, action: str, row_id: str, detail: str) -> None:
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action=action,
                    resource_type="content_review_item", resource_id=row_id, detail=detail[:2000]))


def describe(row: ContentReviewItem) -> str:
    return f"{ITEM_TYPES.get(row.item_type, row.item_type)} {row.title!r} ({row.item_type}:{row.item_id})"


def mark_reviewed(db: Session, user: CurrentUser, row: ContentReviewItem, note: str = "") -> set[str]:
    """Reset the clock: the item's own reasons go, and what it pushed downstream with them."""
    require_review(db, user, row)
    graph, rows = load_graph(db, str(row.id))  # locks and re-reads ``row`` too
    require_review(db, user, row)  # as it is now, locked
    if row.retired:
        raise HTTPException(409, "Retired content is not reviewed")
    at = now()
    key = str(row.id)
    rows[key] = row
    cleared = [r.get("text", "") for r in graph.reasons.get(key, []) if r.get("origin") == key]
    changed = graph.mark_reviewed(key)
    _write_back(graph, rows, changed)
    row.last_reviewed_at = at
    row.last_reviewed_by = _uid(user)
    refresh_clock(row, Cycles(db).for_row(row), at)
    detail = f"{describe(row)}; cleared: {'; '.join(cleared) or 'nothing'}; {len(changed - {key})} downstream"
    if note:
        detail += f"; note: {note}"
    audit(db, user, "content_review.mark_reviewed", key, detail)
    return changed


def assign_reviewer(db: Session, user: CurrentUser, row: ContentReviewItem, reviewer_id: uuid.UUID | None) -> None:
    """Admins, or the instructor who owns the item; never its assigned reviewer, who may only
    mark it reviewed."""
    if not can_assign(db, user, row):
        require_review(db, user, row)  # the shared-content message where it applies
        raise HTTPException(403, "Only an admin or the content's owner can assign its reviewer")
    if row.retired:
        raise HTTPException(409, "Retired content is not reviewed")
    name = "nobody"
    if reviewer_id is not None:
        person = _staff(db, row, reviewer_id, user)
        name = f"{person.display_name} <{person.email}>"
    before = row.reviewer_id
    row.reviewer_id = reviewer_id
    row.revision = (row.revision or 0) + 1
    audit(db, user, "content_review.assign_reviewer", str(row.id),
          f"{describe(row)}; reviewer {before} -> {reviewer_id} ({name})")


def _staff(db: Session, row: ContentReviewItem, person_id: uuid.UUID, user: CurrentUser) -> User:
    """An admin or instructor of the item's tenant (the caller's, for shared content)."""
    tenant = row.tenant_id or tenant_uuid(user)
    person = db.query(User).filter(User.id == person_id, User.tenant_id == tenant,
                                   User.deleted_at.is_(None)).first()
    if person is None:
        raise HTTPException(404, "User not found")
    if person.role not in REVIEWER_ROLES:
        raise HTTPException(422, "A reviewer or owner is an admin or an instructor")
    return person


def retire(db: Session, user: CurrentUser, row: ContentReviewItem, reason: str) -> None:
    """Admins only. What the item made stale downstream is cleared with it: a retired item is
    no source, and it cannot be reviewed to clear it later."""
    require_admin(db, user, row)
    key = str(row.id)
    graph, rows = load_graph(db, key)  # locks and re-reads ``row`` too
    if row.retired:
        raise HTTPException(409, "Already retired")
    rows[key] = row
    changed = graph.mark_reviewed(key)
    _write_back(graph, rows, changed - {key})
    row.retired, row.retired_at, row.retired_by = True, now(), _uid(user)
    row.revision = (row.revision or 0) + 1
    audit(db, user, "content_review.retire", key,
          f"{describe(row)}; reason: {reason}; {len(changed - {key})} downstream cleared")


def update_item(db: Session, user: CurrentUser, row: ContentReviewItem, body: ContentItemUpdateIn) -> None:
    require_admin(db, user, row)
    changes = []
    if body.clear_owner or body.owner_id is not None:
        owner = None if body.clear_owner else _staff(db, row, body.owner_id, user).id
        changes.append(f"owner {row.owner_id} -> {owner}")
        row.owner_id = owner
    if body.theme_ids is not None:
        # Shared content carries only platform-wide themes: every tenant sees its tags.
        known = {t.id for t in visible_themes(db, user) if row.tenant_id is not None or t.tenant_id is None}
        unknown = [str(t) for t in body.theme_ids if t not in known]
        if unknown:
            raise HTTPException(422, f"Unknown theme: {', '.join(unknown)}")
        themes = [str(t) for t in dict.fromkeys(body.theme_ids)]
        changes.append(f"themes {row.theme_ids} -> {themes}")
        row.theme_ids = themes
    if body.clear_cycle or body.cycle_days is not None:
        cycle = None if body.clear_cycle else body.cycle_days
        changes.append(f"cycle {row.cycle_days} -> {cycle}")
        row.cycle_days = cycle
    row.revision = (row.revision or 0) + 1
    refresh_clock(row, Cycles(db).for_row(row), now())
    audit(db, user, "content_review.update", str(row.id), f"{describe(row)}; {'; '.join(changes) or 'no change'}")


# ── themes ───────────────────────────────────────────────────────────────────
# ADR 0014 decision 22's themes and the approved mockup's colours (the dot). The migration
# adds them; seed_default_themes adds them where the schema came from create_all instead.
DEFAULT_THEMES = (
    ("Blue team", "#3B5BDB"),
    ("Red team", "#E03131"),
    ("DFIR", "#7950F2"),
    ("Network", "#12A594"),
    ("ICS / OT", "#F59F00"),
    ("Cloud", "#D6336C"),
    ("Malware", "#2F9E44"),
    ("Platform", "#6B7280"),
)


def seed_default_themes(db: Session) -> int:
    """Add the default themes platform-wide when there are no themes at all (a fresh install
    whose schema came from create_all). Never once any exist: an admin's edits and deletions
    stand. Does not commit. Returns how many were added."""
    if db.query(ContentReviewTheme.id).first() is not None:
        return 0
    for i, (name, colour) in enumerate(DEFAULT_THEMES):
        db.add(ContentReviewTheme(id=uuid.uuid4(), tenant_id=None, name=name, colour=colour, ordinal=i))
    db.flush()
    return len(DEFAULT_THEMES)


def visible_themes(db: Session, user: CurrentUser) -> list[ContentReviewTheme]:
    return (db.query(ContentReviewTheme)
            .filter(or_(ContentReviewTheme.tenant_id == tenant_uuid(user), ContentReviewTheme.tenant_id.is_(None)))
            .order_by(ContentReviewTheme.ordinal, ContentReviewTheme.name).all())


def theme_out(t: ContentReviewTheme) -> ThemeOut:
    return ThemeOut(id=t.id, name=t.name, colour=t.colour.upper(), ordinal=t.ordinal, platform_wide=t.tenant_id is None)


def _theme_scope(db: Session, user: CurrentUser, platform_wide: bool) -> uuid.UUID | None:
    if platform_wide:
        if not is_platform_admin(user, db):
            raise HTTPException(403, "Only a platform administrator can change platform-wide settings")
        return None
    return tenant_uuid(user)


def create_theme(db: Session, user: CurrentUser, body: ThemeIn) -> ContentReviewTheme:
    tenant = _theme_scope(db, user, body.platform_wide)
    _unique_theme_name(db, tenant, body.name)
    t = ContentReviewTheme(id=uuid.uuid4(), tenant_id=tenant, name=body.name.strip(), colour=body.colour.upper(),
                           ordinal=body.ordinal)
    db.add(t)
    db.flush()
    _audit_theme(db, user, "content_review.theme_create", t, f"{t.name} {t.colour}")
    return t


def editable_theme(db: Session, user: CurrentUser, theme_id: uuid.UUID) -> ContentReviewTheme:
    t = next((t for t in visible_themes(db, user) if t.id == theme_id), None)
    if t is None:
        raise HTTPException(404, "Theme not found")
    _theme_scope(db, user, t.tenant_id is None)
    return t


def update_theme(db: Session, user: CurrentUser, t: ContentReviewTheme, body: ThemeIn) -> None:
    if body.platform_wide != (t.tenant_id is None):
        raise HTTPException(422, "A theme's scope (tenant or platform-wide) is fixed")
    if body.name.strip().casefold() != t.name.casefold():
        _unique_theme_name(db, t.tenant_id, body.name)
    before = f"{t.name} {t.colour} {t.ordinal}"
    t.name, t.colour, t.ordinal = body.name.strip(), body.colour.upper(), body.ordinal
    _audit_theme(db, user, "content_review.theme_update", t, f"{before} -> {t.name} {t.colour} {t.ordinal}")


def delete_theme(db: Session, user: CurrentUser, t: ContentReviewTheme) -> None:
    tid = str(t.id)
    q = db.query(ContentReviewItem)
    if t.tenant_id is not None:
        q = q.filter(ContentReviewItem.tenant_id == t.tenant_id)
    for row in q.all():  # untag it everywhere it is used
        if tid in (row.theme_ids or []):
            row.theme_ids = [x for x in row.theme_ids if x != tid]
            row.revision = (row.revision or 0) + 1
    _audit_theme(db, user, "content_review.theme_delete", t, f"{t.name} {t.colour}")
    db.delete(t)


def _unique_theme_name(db: Session, tenant: uuid.UUID | None, name: str) -> None:
    q = db.query(ContentReviewTheme).filter(
        ContentReviewTheme.tenant_id.is_(None) if tenant is None else ContentReviewTheme.tenant_id == tenant)
    if any(t.name.casefold() == name.strip().casefold() for t in q.all()):
        raise HTTPException(409, "A theme of that name exists")


def _audit_theme(db: Session, user: CurrentUser, action: str, t: ContentReviewTheme, detail: str) -> None:
    scope = "platform-wide" if t.tenant_id is None else "tenant"
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action=action,
                    resource_type="content_review_theme", resource_id=str(t.id), detail=f"{scope}: {detail}"[:2000]))


# ── cycles ───────────────────────────────────────────────────────────────────
def list_cycles(db: Session, user: CurrentUser) -> list[CycleOut]:
    cycles = Cycles(db)
    out = []
    for item_type in ITEM_TYPES:
        days, source = cycles.get(tenant_uuid(user), item_type)
        out.append(CycleOut(item_type=item_type, days=days, source=source))
    return out


def set_cycle(db: Session, user: CurrentUser, item_type: str, body: CycleIn) -> CycleOut:
    if item_type not in ITEM_TYPES:
        raise HTTPException(404, "Unknown content type")
    if not user_has_permission(user, Permission.CONTENT_REVIEW_ADMIN):
        raise HTTPException(403, "Only an admin can do this")
    tenant = _theme_scope(db, user, body.platform_wide)
    row = db.query(ContentReviewCycle).filter(
        ContentReviewCycle.item_type == item_type,
        ContentReviewCycle.tenant_id.is_(None) if tenant is None else ContentReviewCycle.tenant_id == tenant,
    ).first()
    before = row.days if row else None
    if row is None:
        row = ContentReviewCycle(id=uuid.uuid4(), tenant_id=tenant, item_type=item_type, days=body.days)
        db.add(row)
    row.days = body.days
    db.flush()
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action="content_review.cycle",
                    resource_type="content_review_cycle", resource_id=item_type,
                    detail=f"{'platform-wide' if tenant is None else 'tenant'}: {before} -> {body.days} days"))
    # The due dates it moves change now, not at the next sync.
    cycles, at = Cycles(db), now()
    q = db.query(ContentReviewItem).filter(ContentReviewItem.item_type == item_type,
                                           ContentReviewItem.cycle_days.is_(None))
    if tenant is not None:
        q = q.filter(ContentReviewItem.tenant_id == tenant)
    for item in q.all():
        refresh_clock(item, cycles.for_row(item), at)
    days, source = cycles.get(tenant_uuid(user), item_type)
    return CycleOut(item_type=item_type, days=days, source=source)


# ── output ───────────────────────────────────────────────────────────────────
class Presenter:
    """Builds the wire shapes for one caller, with names and cycles looked up once."""

    def __init__(self, db: Session, user: CurrentUser, rows: Iterable[ContentReviewItem]):
        self.db, self.user, self.at = db, user, now()
        self.cycles = Cycles(db)
        rows = list(rows)
        ids = {i for r in rows for i in (r.owner_id, r.reviewer_id, r.last_reviewed_by) if i is not None}
        # Names of the caller's own tenant's people only: a shared item's owner may be another tenant's.
        self.names = {u.id: u.display_name for u in db.query(User).filter(
            User.id.in_(ids), User.tenant_id == tenant_uuid(user)).all()} if ids else {}
        self.platform_admin = is_platform_admin(user, db)

    def _can_admin(self, row: ContentReviewItem) -> bool:
        if not user_has_permission(self.user, Permission.CONTENT_REVIEW_ADMIN):
            return False
        return self.platform_admin if row.tenant_id is None else row.tenant_id == tenant_uuid(self.user)

    def _can_review(self, row: ContentReviewItem) -> bool:
        return self._can_admin(row) or (
            user_has_permission(self.user, Permission.CONTENT_REVIEW_OWN) and row.tenant_id is not None
            and row.tenant_id == tenant_uuid(self.user) and owns(self.user, row))

    def _can_assign(self, row: ContentReviewItem) -> bool:
        me = _uid(self.user)
        return self._can_admin(row) or (
            user_has_permission(self.user, Permission.CONTENT_REVIEW_OWN) and row.tenant_id is not None
            and row.tenant_id == tenant_uuid(self.user) and me is not None and row.owner_id == me)

    def _person(self, person_id: uuid.UUID | None) -> uuid.UUID | None:
        return person_id if person_id in self.names else None

    def item(self, row: ContentReviewItem) -> ContentItemOut:
        return ContentItemOut(**self._fields(row))

    def _fields(self, row: ContentReviewItem) -> dict:
        reasons = [StaleReasonOut(kind=r.get("kind"), text=r.get("text", ""), origin_id=str(r.get("origin", "")),
                                  source=str(r.get("source") or ""), at=r.get("at"))
                   for r in (row.stale_reasons or []) if isinstance(r, dict)
                   and r.get("kind") in ("time", "release", "vulnerability", "upstream")]
        state, days = staleness.clock(_utc(row.due_at), self.at, stale=bool(reasons))
        return dict(
            id=row.id, item_type=row.item_type, item_id=row.item_id, title=row.title, shared=row.tenant_id is None,
            # People of another tenant (a shared item's) are not shown: neither name nor id.
            owner_id=self._person(row.owner_id), owner_name=self.names.get(row.owner_id),
            reviewer_id=self._person(row.reviewer_id), reviewer_name=self.names.get(row.reviewer_id),
            theme_ids=[uuid.UUID(t) for t in (row.theme_ids or []) if _is_uuid(t)],
            cycle_days=self.cycles.for_row(row), cycle_override=row.cycle_days,
            last_reviewed_at=row.last_reviewed_at, last_reviewed_by_name=self.names.get(row.last_reviewed_by),
            due_at=row.due_at, clock=ContentClockOut(state=state, days=days), stale=bool(reasons),
            stale_reasons=reasons, retired=row.retired, retired_at=row.retired_at,
            can_review=not row.retired and self._can_review(row),
            can_assign=not row.retired and self._can_assign(row), can_admin=self._can_admin(row),
        )

    def detail(self, row: ContentReviewItem) -> ContentItemDetailOut:
        q = visible(self.db, self.user)
        dep_ids = [e.depends_on_id for e in self.db.query(ContentReviewEdge).filter(ContentReviewEdge.item_id == row.id)]
        user_ids = [e.item_id for e in self.db.query(ContentReviewEdge).filter(ContentReviewEdge.depends_on_id == row.id)]

        def briefs(ids):
            if not ids:
                return []
            found = q.filter(ContentReviewItem.id.in_(ids)).order_by(ContentReviewItem.item_type,
                                                                      ContentReviewItem.title).all()
            return [ContentItemBrief(id=r.id, item_type=r.item_type, title=r.title, stale=r.stale, retired=r.retired)
                    for r in found]

        return ContentItemDetailOut(**self._fields(row), depends_on=briefs(dep_ids), used_by=briefs(user_ids))


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
