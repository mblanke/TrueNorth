"""TrueNorth Range — In-app notifications for the signed-in user.

Every endpoint works on the caller's own notifications only (user id and tenant both
in the query); someone else's notification is a 404.

=========================================  ==========================
Endpoint                                   Permission(s)
=========================================  ==========================
GET    /notifications                      signed in
GET    /notifications/unread-count         signed in
POST   /notifications/{id}/read            signed in (own only)
POST   /notifications/read-all             signed in
=========================================  ==========================
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..notifications.models import UserNotification
from ..tenancy import tenant_uuid

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    title: str
    message: str
    level: str
    read: bool
    link: str | None = None
    kind: str = "info"
    created_at: datetime


class NotificationUnreadCountOut(BaseModel):
    unread: int


def _mine(db: Session, user: CurrentUser):
    return db.query(UserNotification).filter(
        UserNotification.user_id == uuid.UUID(user.id), UserNotification.tenant_id == tenant_uuid(user)
    )


def _out(n: UserNotification) -> NotificationOut:
    try:
        data = json.loads(n.data_json or "{}")
    except ValueError:
        data = {}
    return NotificationOut(
        id=n.id,
        title=n.title,
        message=n.message,
        level=n.level,
        read=n.read,
        link=data.get("link"),
        kind=data.get("kind") or "info",
        created_at=n.created_at,
    )


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[NotificationOut]:
    q = _mine(db, user)
    if unread_only:
        q = q.filter(UserNotification.read.is_(False))
    return [_out(n) for n in q.order_by(UserNotification.created_at.desc()).limit(limit).all()]


@router.get("/unread-count", response_model=NotificationUnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> NotificationUnreadCountOut:
    return NotificationUnreadCountOut(unread=_mine(db, user).filter(UserNotification.read.is_(False)).count())


@router.post("/read-all", response_model=NotificationUnreadCountOut)
def mark_all_read(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> NotificationUnreadCountOut:
    _mine(db, user).filter(UserNotification.read.is_(False)).update(
        {"read": True, "read_at": datetime.now(UTC)}, synchronize_session=False
    )
    db.commit()
    return NotificationUnreadCountOut(unread=0)


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> NotificationOut:
    n = (
        _mine(db, user).filter(UserNotification.id == notification_id).first()
    )  # tenant-safe: _mine filters user and tenant
    if not n:
        raise HTTPException(404, "Notification not found")
    if not n.read:
        n.read = True
        n.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(n)
    return _out(n)
