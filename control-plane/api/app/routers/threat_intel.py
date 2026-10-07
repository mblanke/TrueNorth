"""TrueNorth Range — Threat Intelligence feed management router.

Feeds are pulled through their backend (``app/threat_intel_backends``, chosen by the
feed's ``feed_type``) and stored as the feed's indicators in the feed's tenant
(``app/threat_intel_sync``):

  POST /threat-intel/feeds/{id}/pull     fetch the feed's URL now
  POST /threat-intel/feeds/{id}/upload   read an uploaded file as the feed (multipart ``file``)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import threat_intel_sync
from ..auth import CurrentUser
from ..db import get_db, not_deleted
from ..models import AuditLog, ThreatIndicator, ThreatIntelFeed
from ..rbac import Permission, require_permission
from ..schemas import (
    ThreatIndicatorOut,
    ThreatIntelFeedIn,
    ThreatIntelFeedOut,
    ThreatIntelFeedUpdate,
)
from ..threat_intel_backends import (
    FeedMalformedError,
    FeedSourceError,
    FeedUnreachableError,
    get_feed_backend,
)
from ..threat_intel_backends.csv_feed import max_bytes

router = APIRouter(prefix="/threat-intel", tags=["threat-intel"])
logger = logging.getLogger("truenorth.api.threat_intel")


# ── Feeds ──────────────────────────────────────────────────────────────


@router.get("/feeds", response_model=list[ThreatIntelFeedOut])
async def list_feeds(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[ThreatIntelFeedOut]:
    query = db.query(ThreatIntelFeed).filter(ThreatIntelFeed.tenant_id == user.tenant_id)
    query = not_deleted(query, ThreatIntelFeed)
    return query.order_by(ThreatIntelFeed.name).all()


@router.post("/feeds", response_model=ThreatIntelFeedOut, status_code=status.HTTP_201_CREATED)
async def create_feed(
    payload: ThreatIntelFeedIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> ThreatIntelFeedOut:
    feed = ThreatIntelFeed(
        **payload.model_dump(),
        tenant_id=user.tenant_id,
    )
    db.add(feed)
    db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            tenant_id=user.tenant_id,
            action="threat_intel.feed.created",
            resource_type="threat_intel_feed",
            resource_id=str(feed.id),
            detail=f"Created feed: {feed.name} ({feed.feed_type})",
        )
    )
    db.commit()
    db.refresh(feed)
    logger.info("Feed created: %s (%s) by user %s", feed.name, feed.feed_type, user.id)
    return feed


@router.get("/feeds/{feed_id}", response_model=ThreatIntelFeedOut)
async def get_feed(
    feed_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> ThreatIntelFeedOut:
    feed = (
        db.query(ThreatIntelFeed)
        .filter(
            ThreatIntelFeed.id == feed_id,
            ThreatIntelFeed.tenant_id == user.tenant_id,
            ThreatIntelFeed.deleted_at.is_(None),
        )
        .first()
    )
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    return feed


@router.patch("/feeds/{feed_id}", response_model=ThreatIntelFeedOut)
async def update_feed(
    payload: ThreatIntelFeedUpdate,
    feed_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> ThreatIntelFeedOut:
    feed = (
        db.query(ThreatIntelFeed)
        .filter(
            ThreatIntelFeed.id == feed_id,
            ThreatIntelFeed.tenant_id == user.tenant_id,
            ThreatIntelFeed.deleted_at.is_(None),
        )
        .first()
    )
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(feed, field, value)
    db.commit()
    db.refresh(feed)
    return feed


@router.delete("/feeds/{feed_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_feed(
    feed_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
):
    feed = (
        db.query(ThreatIntelFeed)
        .filter(
            ThreatIntelFeed.id == feed_id,
            ThreatIntelFeed.tenant_id == user.tenant_id,
            ThreatIntelFeed.deleted_at.is_(None),
        )
        .first()
    )
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    feed.soft_delete()
    db.add(
        AuditLog(
            user_id=user.id,
            tenant_id=user.tenant_id,
            action="threat_intel.feed.deleted",
            resource_type="threat_intel_feed",
            resource_id=str(feed.id),
        )
    )
    db.commit()


# ── Pulling a feed ─────────────────────────────────────────────────────


class FeedRejectionOut(BaseModel):
    row: int
    reason: str


class FeedPullOut(BaseModel):
    """What a pull did. ``rejections`` lists at most the first 50 rejected rows."""

    feed: ThreatIntelFeedOut
    status: str
    created: int
    updated: int
    deactivated: int
    rejected: int
    rejections: list[FeedRejectionOut]


def _owned_feed(db: Session, feed_id: uuid.UUID, user: CurrentUser) -> ThreatIntelFeed:
    feed = (
        db.query(ThreatIntelFeed)
        .filter(
            ThreatIntelFeed.id == feed_id,
            ThreatIntelFeed.tenant_id == user.tenant_id,
            ThreatIntelFeed.deleted_at.is_(None),
        )
        .first()
    )
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    if not feed.is_enabled:
        raise HTTPException(status_code=409, detail="Feed is disabled; enable it to pull")
    return feed


def _pull(db: Session, feed: ThreatIntelFeed, user: CurrentUser, content: bytes | None) -> FeedPullOut:
    try:
        backend = get_feed_backend(feed.feed_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if content is not None and not backend.accepts_upload:
        raise HTTPException(status_code=422, detail=f"Feeds of type {feed.feed_type!r} cannot be uploaded")

    failure: tuple[int, str, str] | None = None
    try:
        pull = backend.fetch(feed.url, content=content)
    except FeedUnreachableError as exc:
        failure = (502, "unreachable", str(exc))
    except FeedMalformedError as exc:
        failure = (422, "malformed", str(exc))
    except FeedSourceError as exc:
        failure = (422, "refused", str(exc))
    if failure is not None:
        code, kind, message = failure
        threat_intel_sync.record_failure(feed, kind)
        db.commit()  # the feed list shows the failed pull
        logger.warning("Feed %s pull failed (%s): %s", feed.id, kind, message)
        raise HTTPException(status_code=code, detail=message)

    result = threat_intel_sync.apply_pull(db, feed, pull)
    db.add(
        AuditLog(
            user_id=user.id,
            tenant_id=user.tenant_id,
            action="threat_intel.feed.pulled",
            resource_type="threat_intel_feed",
            resource_id=str(feed.id),
            detail=(
                f"{'upload' if content is not None else 'fetch'}: {result.created} created, {result.updated} updated, "
                f"{result.deactivated} deactivated, {result.rejected} rejected"
            ),
        )
    )
    db.commit()
    db.refresh(feed)
    return FeedPullOut(
        feed=ThreatIntelFeedOut.model_validate(feed),
        status=result.status,
        created=result.created,
        updated=result.updated,
        deactivated=result.deactivated,
        rejected=result.rejected,
        rejections=[FeedRejectionOut(row=r.row, reason=r.reason) for r in result.rejections],
    )


@router.post("/feeds/{feed_id}/pull", response_model=FeedPullOut)
def pull_feed(
    feed_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> FeedPullOut:
    """Fetch the feed's URL now and store its indicators (502 when the feed cannot be reached)."""
    return _pull(db, _owned_feed(db, feed_id, user), user, content=None)


@router.post("/feeds/{feed_id}/upload", response_model=FeedPullOut)
def upload_feed(
    feed_id: uuid.UUID = Path(...),
    file: UploadFile = File(..., description="The feed's content, e.g. a CSV"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> FeedPullOut:
    """Read an uploaded file as the feed's current content and store its indicators."""
    feed = _owned_feed(db, feed_id, user)
    limit = max_bytes()
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail=f"The upload is larger than {limit} bytes")
    return _pull(db, feed, user, content=content)


# ── Indicators ─────────────────────────────────────────────────────────


@router.get("/indicators", response_model=list[ThreatIndicatorOut])
async def search_indicators(
    indicator_type: str | None = Query(None),
    value: str | None = Query(None),
    severity: str | None = Query(None),
    min_confidence: int | None = Query(None, ge=0, le=100),
    feed_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[ThreatIndicatorOut]:
    query = db.query(ThreatIndicator).filter(
        ThreatIndicator.tenant_id == user.tenant_id,
        ThreatIndicator.is_active.is_(True),
    )
    if indicator_type:
        query = query.filter(ThreatIndicator.indicator_type == indicator_type)
    if value:
        query = query.filter(ThreatIndicator.value.ilike(f"%{value}%"))
    if severity:
        query = query.filter(ThreatIndicator.severity == severity)
    if min_confidence is not None:
        query = query.filter(ThreatIndicator.confidence >= min_confidence)
    if feed_id:
        query = query.filter(ThreatIndicator.feed_id == feed_id)
    return query.order_by(ThreatIndicator.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/indicators/{indicator_id}", response_model=ThreatIndicatorOut)
async def get_indicator(
    indicator_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> ThreatIndicatorOut:
    indicator = (
        db.query(ThreatIndicator)
        .filter(
            ThreatIndicator.id == indicator_id,
            ThreatIndicator.tenant_id == user.tenant_id,
        )
        .first()
    )
    if not indicator:
        raise HTTPException(status_code=404, detail="Indicator not found")
    return indicator


@router.get("/feeds/{feed_id}/indicators", response_model=list[ThreatIndicatorOut])
async def list_feed_indicators(
    feed_id: uuid.UUID = Path(...),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[ThreatIndicatorOut]:
    feed = (
        db.query(ThreatIntelFeed)
        .filter(
            ThreatIntelFeed.id == feed_id,
            ThreatIntelFeed.tenant_id == user.tenant_id,
            ThreatIntelFeed.deleted_at.is_(None),
        )
        .first()
    )
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    return (
        db.query(ThreatIndicator)
        .filter(ThreatIndicator.feed_id == feed_id)
        .order_by(ThreatIndicator.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
