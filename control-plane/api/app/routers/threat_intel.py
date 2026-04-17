"""TrueNorth Range — Threat Intelligence feed management router."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

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
