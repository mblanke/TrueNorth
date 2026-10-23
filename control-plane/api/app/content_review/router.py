"""The content clock's API (ADR 0014 decisions 22-24, issue #286).

=====================================================  ============================================
Endpoint                                               Permission
=====================================================  ============================================
GET    /content-review/items?start&end&theme_id        content_review:read
GET    /content-review/stale                           content_review:read
GET    /content-review/items/{id}                      content_review:read
POST   /content-review/items/{id}/review               admin; instructor on content they own
PUT    /content-review/items/{id}/reviewer             admin; instructor on content they own (not its reviewer)
POST   /content-review/items/{id}/retire               content_review:admin
PATCH  /content-review/items/{id}                      content_review:admin (owner, themes, cycle)
GET    /content-review/themes                          content_review:read
POST   /content-review/themes                          content_review:admin (+ platform admin: platform-wide)
PUT    /content-review/themes/{id}                     content_review:admin (+ platform admin: platform-wide)
DELETE /content-review/themes/{id}                     content_review:admin (+ platform admin: platform-wide)
GET    /content-review/cycles                          content_review:read
PUT    /content-review/cycles/{item_type}              content_review:admin (+ platform admin: platform-wide)
=====================================================  ============================================

Every read is the caller's tenant's content plus the shared catalogue; another tenant's is
not found (404). Shared content is changed only by a platform administrator. The rows are
kept current by the worker's periodic sync (``worker/content_review_sync.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_any_permission, require_permission
from . import service
from .models import ContentReviewItem
from .schemas import (
    AssignReviewerIn,
    ContentItemDetailOut,
    ContentItemListOut,
    ContentItemOut,
    ContentItemUpdateIn,
    CycleIn,
    CycleOut,
    MarkReviewedIn,
    RetireIn,
    ThemeIn,
    ThemeOut,
)

router = APIRouter(prefix="/content-review", tags=["content-review"])

_READ = require_permission(Permission.CONTENT_REVIEW_READ)
_REVIEW = require_any_permission(Permission.CONTENT_REVIEW_OWN, Permission.CONTENT_REVIEW_ADMIN)
_ADMIN = require_permission(Permission.CONTENT_REVIEW_ADMIN)
MAX_WINDOW = timedelta(days=400)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ── items ────────────────────────────────────────────────────────────────────
@router.get("/items", response_model=ContentItemListOut, operation_id="list_content_review_items",
            summary="Content due for review in a date window")
def list_items(
    start: datetime = Query(..., description="Window start (inclusive)"),
    end: datetime = Query(..., description="Window end (exclusive)"),
    theme_id: list[str] = Query(default=[], description="Only these themes (a theme id, or 'none' for untagged)"),
    include_retired: bool = Query(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> ContentItemListOut:
    """Each item on its review-due date, with its stale reasons and clock. ``theme_counts``
    counts the window before the theme filter, for the theme chips."""
    start, end = _utc(start), _utc(end)
    if end <= start or end - start > MAX_WINDOW:
        raise HTTPException(422, "end must be after start, and the window at most 400 days")
    q = service.visible(db, user).filter(ContentReviewItem.due_at >= start, ContentReviewItem.due_at < end)
    if not include_retired:
        q = q.filter(ContentReviewItem.retired.is_(False))
    rows = q.order_by(ContentReviewItem.due_at, ContentReviewItem.title).all()
    counts: dict[str, int] = {}
    for r in rows:
        for t in (r.theme_ids or []) or ["none"]:
            counts[str(t)] = counts.get(str(t), 0) + 1
    wanted = set(theme_id)
    if wanted:
        rows = [r for r in rows if wanted & ({str(t) for t in (r.theme_ids or [])} or {"none"})]
    present = service.Presenter(db, user, rows)
    return ContentItemListOut(items=[present.item(r) for r in rows], theme_counts=counts, total=len(rows))


@router.get("/stale", response_model=list[ContentItemOut], operation_id="list_stale_content",
            summary="Content that is stale now")
def stale_now(
    theme_id: list[str] = Query(default=[], description="Only these themes (a theme id, or 'none')"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> list[ContentItemOut]:
    """Stale, not retired, the longest-overdue first."""
    rows = (service.visible(db, user)
            .filter(ContentReviewItem.stale.is_(True), ContentReviewItem.retired.is_(False))
            .order_by(ContentReviewItem.due_at, ContentReviewItem.title).all())
    wanted = set(theme_id)
    if wanted:  # theme ids live in a JSON list: filtered here, then limited
        rows = [r for r in rows if wanted & ({str(t) for t in (r.theme_ids or [])} or {"none"})]
    rows = rows[:limit]
    present = service.Presenter(db, user, rows)
    return [present.item(r) for r in rows]


@router.get("/items/{item_id}", response_model=ContentItemDetailOut, operation_id="get_content_review_item",
            summary="One content item: its clock, what it depends on and what uses it")
def get_item(item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
             user: CurrentUser = Depends(_READ)) -> ContentItemDetailOut:
    return _detail(db, user, item_id)


def _detail(db: Session, user: CurrentUser, item_id: uuid.UUID) -> ContentItemDetailOut:
    row = service.get_visible(db, user, item_id)
    return service.Presenter(db, user, [row]).detail(row)


@router.post("/items/{item_id}/review", response_model=ContentItemDetailOut,
             operation_id="mark_content_reviewed", summary="Mark reviewed: reset the item's clock")
def mark_reviewed(body: MarkReviewedIn, item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                  user: CurrentUser = Depends(_REVIEW)) -> ContentItemDetailOut:
    """Clears the item's own stale reasons (an overdue review, a release or vulnerability
    flag) and what they made stale downstream. Reasons it inherited stay until their source
    is reviewed. Audit-logged."""
    row = service.get_visible(db, user, item_id)  # mark_reviewed locks every row it rewrites, in id order
    service.mark_reviewed(db, user, row, body.note)
    db.commit()
    return _detail(db, user, item_id)


@router.put("/items/{item_id}/reviewer", response_model=ContentItemDetailOut,
            operation_id="assign_content_reviewer", summary="Assign (or clear) the item's reviewer")
def assign_reviewer(body: AssignReviewerIn, item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                    user: CurrentUser = Depends(_REVIEW)) -> ContentItemDetailOut:
    """The reviewer is an admin or instructor of the item's tenant. Audit-logged."""
    row = service.get_visible(db, user, item_id, lock=True)
    service.assign_reviewer(db, user, row, body.reviewer_id)
    db.commit()
    return _detail(db, user, item_id)


@router.post("/items/{item_id}/retire", response_model=ContentItemDetailOut, operation_id="retire_content_item",
             summary="Retire the item: it leaves the Content view and the stale list")
def retire(body: RetireIn, item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
           user: CurrentUser = Depends(_ADMIN)) -> ContentItemDetailOut:
    """Admins only (a platform administrator for shared content). The content itself is not
    deleted. Audit-logged, with the reason."""
    row = service.get_visible(db, user, item_id, lock=True)
    service.retire(db, user, row, body.reason)
    db.commit()
    return _detail(db, user, item_id)


@router.patch("/items/{item_id}", response_model=ContentItemDetailOut, operation_id="update_content_review_item",
              summary="Set the item's owner, themes or own review cycle")
def update_item(body: ContentItemUpdateIn, item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                user: CurrentUser = Depends(_ADMIN)) -> ContentItemDetailOut:
    row = service.get_visible(db, user, item_id, lock=True)
    service.update_item(db, user, row, body)
    db.commit()
    return _detail(db, user, item_id)


# ── themes ───────────────────────────────────────────────────────────────────
@router.get("/themes", response_model=list[ThemeOut], operation_id="list_content_themes",
            summary="Theme tags (the tenant's and the platform-wide ones)")
def list_themes(db: Session = Depends(get_db), user: CurrentUser = Depends(_READ)) -> list[ThemeOut]:
    return [service.theme_out(t) for t in service.visible_themes(db, user)]


@router.post("/themes", response_model=ThemeOut, status_code=201, operation_id="create_content_theme",
             summary="Add a theme tag")
def create_theme(body: ThemeIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> ThemeOut:
    t = service.create_theme(db, user, body)
    db.commit()
    return service.theme_out(t)


@router.put("/themes/{theme_id}", response_model=ThemeOut, operation_id="update_content_theme",
            summary="Rename or recolour a theme tag")
def update_theme(body: ThemeIn, theme_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                 user: CurrentUser = Depends(_ADMIN)) -> ThemeOut:
    t = service.editable_theme(db, user, theme_id)
    service.update_theme(db, user, t, body)
    db.commit()
    return service.theme_out(t)


@router.delete("/themes/{theme_id}", status_code=204, response_class=Response, operation_id="delete_content_theme",
               summary="Delete a theme tag (it is removed from every item)")
def delete_theme(theme_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                 user: CurrentUser = Depends(_ADMIN)) -> None:
    service.delete_theme(db, user, service.editable_theme(db, user, theme_id))
    db.commit()


# ── cycles ───────────────────────────────────────────────────────────────────
@router.get("/cycles", response_model=list[CycleOut], operation_id="list_content_review_cycles",
            summary="The review cycle per content type")
def list_cycles(db: Session = Depends(get_db), user: CurrentUser = Depends(_READ)) -> list[CycleOut]:
    return service.list_cycles(db, user)


@router.put("/cycles/{item_type}", response_model=CycleOut, operation_id="set_content_review_cycle",
            summary="Set a content type's review cycle (90 to 120 days)")
def set_cycle(body: CycleIn, item_type: str = Path(...), db: Session = Depends(get_db),
              user: CurrentUser = Depends(_ADMIN)) -> CycleOut:
    out = service.set_cycle(db, user, item_type, body)
    db.commit()
    return out
