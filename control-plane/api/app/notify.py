"""Write in-app notifications.

``notify()`` adds one ``Notification`` row per recipient to the caller's session, so a
notification commits (or rolls back) with the change it reports. The person who caused
the change is never notified of it, and each recipient gets one row however often they
appear in ``user_ids``. Recipients must be users of ``tenant_id``; callers look them up
with the tenant predicate, and this function does not widen that.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from .models import User, UserRole
from .models_notifications import Notification

STAFF_ROLES = (UserRole.admin, UserRole.instructor, UserRole.range_ops)


def notify(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_ids: Iterable[uuid.UUID | None],
    actor_id: uuid.UUID | None,
    title: str,
    message: str = "",
    link: str | None = None,
    kind: str = "info",
) -> int:
    """Queue a notification for each recipient except the actor; returns how many."""
    data = json.dumps({"link": link, "kind": kind})
    seen: set[uuid.UUID] = set()
    for uid in user_ids:
        if uid is None or uid == actor_id or uid in seen:
            continue
        seen.add(uid)
        db.add(Notification(user_id=uid, tenant_id=tenant_id, title=title[:255], message=message, data_json=data))
    return len(seen)


def staff_ids(db: Session, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Active staff (admin, instructor, range ops) of one tenant."""
    rows = (
        db.query(User.id)
        .filter(
            User.tenant_id == tenant_id, User.role.in_(STAFF_ROLES), User.deleted_at.is_(None), User.is_active.is_(True)
        )
        .all()
    )
    return [r.id for r in rows]


def existing_user_ids(db: Session, tenant_id: uuid.UUID, ids: Iterable[uuid.UUID | None]) -> list[uuid.UUID]:
    """Keep only ids that are real users of the tenant (a reporter may have no users row in dev)."""
    wanted = {i for i in ids if i}
    if not wanted:
        return []
    rows = db.query(User.id).filter(User.id.in_(wanted), User.tenant_id == tenant_id).all()
    return [r.id for r in rows]
