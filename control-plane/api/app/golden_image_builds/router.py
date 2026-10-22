"""Golden-image builds: list them, and promote one (ADR 0013 §2).

GET  /golden-images/{image_id}/builds                     INFRA_READ
POST /golden-images/{image_id}/builds/{build_id}/promote  platform administrator only; audited

The registry is platform-wide: one promoted build serves every tenant's ranges, so only
the platform administrator (rbac.require_platform_admin) promotes, never a tenant's own
admin. Builds are registered through PATCH /golden-images/{image_id} (``build``), the
template kit's register step (app/routers/golden_images.py), also platform admin only.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, GoldenImage, User
from ..rbac import Permission, is_platform_admin, require_permission, require_platform_admin
from . import service
from .schemas import GoldenImageBuildOut, GoldenImageBuildPromoteIn

router = APIRouter(prefix="/golden-images", tags=["golden-images"])


class GoldenImageAuditEntry(BaseModel):
    timestamp: datetime | None
    action: str
    actor: str
    detail: dict


class GoldenImageBuildsOut(BaseModel):
    image_id: str
    catalogue_id: str
    promoted_build_id: str | None
    # Whether the caller may promote (the platform administrator); the API decides.
    can_promote: bool = False
    builds: list[GoldenImageBuildOut]
    audit: list[GoldenImageAuditEntry]


def _image(db: Session, image_id: str) -> GoldenImage:
    # tenant-safe: the golden-image registry is platform infrastructure shared by every
    # tenant (see app/routers/golden_images.py update_image); reads need infra:read.
    img = db.query(GoldenImage).filter(GoldenImage.id == image_id, GoldenImage.deleted_at.is_(None)).one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="golden image not found")
    return img


def _audit(db: Session, img: GoldenImage, limit: int = 50) -> list[GoldenImageAuditEntry]:
    rows = (db.query(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id)
            .filter(AuditLog.resource_type == service.AUDIT_RESOURCE, AuditLog.resource_id == str(img.id))
            .order_by(AuditLog.timestamp.desc()).limit(limit).all())
    out = []
    for entry, email in rows:
        try:
            detail = json.loads(entry.detail or "{}")
        except ValueError:
            detail = {"text": entry.detail}
        out.append(GoldenImageAuditEntry(timestamp=entry.timestamp, action=entry.action, actor=email or "",
                                         detail=detail if isinstance(detail, dict) else {"value": detail}))
    return out


def _builds_out(db: Session, img: GoldenImage, user: CurrentUser) -> GoldenImageBuildsOut:
    rows = service.builds(db, img)
    promoted = next((r.build_id for r in rows if r.promoted), None)
    return GoldenImageBuildsOut(image_id=str(img.id), catalogue_id=img.catalogue_id, promoted_build_id=promoted,
                                can_promote=is_platform_admin(user, db),
                                builds=[service.build_out(r, db) for r in rows], audit=_audit(db, img))


@router.get("/{image_id}/builds", response_model=GoldenImageBuildsOut)
def list_image_builds(
    image_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INFRA_READ)),
) -> GoldenImageBuildsOut:
    """The image's registered builds (newest first), which one is promoted, and its audit trail.

    **Permission: infra:read**"""
    return _builds_out(db, _image(db, image_id), user)


@router.post("/{image_id}/builds/{build_id}/promote", response_model=GoldenImageBuildsOut)
def promote_image_build(
    image_id: str,
    build_id: str,
    body: GoldenImageBuildPromoteIn = Body(default_factory=GoldenImageBuildPromoteIn),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_platform_admin()),
) -> GoldenImageBuildsOut:
    """Make this build the one every new range clones (ADR 0013 §2). Audited.

    Ranges then require this build's copy on every host they are placed on; a host
    without it is not used, and a build with no host that has it fails. A build that names
    no host copy is refused (422); a concurrent promotion of the same image is 409.

    **Platform administrator only**: the registry serves every tenant."""
    img = _image(db, image_id)
    try:
        service.promote(db, img, build_id, user, reason=body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.BuildNotPromotableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.BuildConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _builds_out(db, img, user)
