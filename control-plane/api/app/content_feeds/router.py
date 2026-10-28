"""The content feeds' API (ADR 0014 decision 25, issue #287).

=====================================  ===============================================
Endpoint                               Permission
=====================================  ===============================================
GET   /content-feeds/status            content_review:admin
POST  /content-feeds/bundle            content_review:admin + platform administrator
=====================================  ===============================================

The status names no tenant's content: the sources' last pull or import, their errors, the
entry count, and how many items the caller can see the feeds have flagged. A bundle's
entries flag shared content in every tenant, so only a platform administrator imports one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import service
from .schemas import FeedImportOut, FeedStatusOut

router = APIRouter(prefix="/content-feeds", tags=["content-feeds"])

_ADMIN = require_permission(Permission.CONTENT_REVIEW_ADMIN)


@router.get("/status", response_model=FeedStatusOut, operation_id="get_content_feeds_status",
            summary="The release and vulnerability feeds: mode, last pull or import, errors")
def get_status(db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> FeedStatusOut:
    return service.status(db, user)


@router.post("/bundle", response_model=FeedImportOut, operation_id="import_content_feed_bundle",
             summary="Import a signed feed bundle (air-gapped sites)")
def import_bundle(file: UploadFile = File(..., description="The signed feed bundle (JSON)"),
                  db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> FeedImportOut:
    """Verified against the site's configured public key before anything is stored; an
    unsigned or mismatched bundle is refused (422) and the refusal is recorded. Entries
    become stale flags at the content clock's next sync (hourly). Audit-logged. A plain
    ``def``: parsing and storing up to 100,000 entries runs in the threadpool, not on the
    event loop."""
    if not service.can_import(db, user):
        raise HTTPException(403, "Only a platform administrator imports feed bundles")
    raw = file.file.read(service.MAX_BUNDLE_BYTES + 1)
    try:
        out = service.import_bundle(db, user, raw)
    except service.BundleRejectedError as exc:
        db.commit()  # the refusal is on the status panel and in the audit log
        raise HTTPException(exc.status, exc.message) from None
    db.commit()
    return out
