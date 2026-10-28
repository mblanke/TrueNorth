"""The content port's API (owner decision 2026-10-27; docs/content-port.md).

==============================================================  =========================================
Endpoint                                                        Permission
==============================================================  =========================================
GET   /content-port/items/{item_type}/{item_id}/proven          content_review:read (own or shared content)
GET   /content-port/items                                       platform admin + content_review:admin
GET   /content-port/signing-key                                 platform admin + content_review:admin
POST  /content-port/export                                      platform admin + content_review:admin
POST  /content-port/preview                                     platform admin + content_review:admin
POST  /content-port/import                                      platform admin + content_review:admin
==============================================================  =========================================

Export answers the signed bundle as a JSON download, or 409 listing every selected or
needed item that is not proven. Preview and import read an uploaded bundle (at most 50 MB,
the edge's upload limit) and verify it against the configured trusted keys before anything
else (409 when none is configured). Import refuses a bundle imported before (409) and
overwrites a conflicting item only when its key is listed in ``overwrite``.
"""

from __future__ import annotations

import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import bundle as bnd
from . import service
from .schemas import PortExportIn, PortImportOut, PortPreviewOut, PortSigningKeyOut, ProvenStatusOut

router = APIRouter(prefix="/content-port", tags=["content-port"])

_READ = require_permission(Permission.CONTENT_REVIEW_READ)
_ADMIN = require_permission(Permission.CONTENT_REVIEW_ADMIN)


def _raw(file: UploadFile) -> bytes:
    return file.file.read(bnd.MAX_BUNDLE_BYTES + 1)


@router.get("/items/{item_type}/{item_id}/proven", response_model=ProvenStatusOut,
            operation_id="get_content_proven_status",
            summary="Whether a course, range template or scenario is proven, with reasons and evidence")
def get_proven(item_type: Literal["course", "range_template", "scenario"] = Path(...),
               item_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
               user: CurrentUser = Depends(_READ)) -> ProvenStatusOut:
    """Computed now: a range built from it reached Certified, a class or exercise using it
    finished with passing evidence, and the content clock does not call it stale. Another
    tenant's private content is not found (404)."""
    return service.item_status(db, user, item_type, item_id)


@router.get("/items", response_model=list[ProvenStatusOut], operation_id="list_portable_content",
            summary="Every course, range template and scenario that could be ported, with its proven status")
def list_items(db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> list[ProvenStatusOut]:
    service.require_port(db, user)
    return service.portable(db, user)


@router.get("/signing-key", response_model=PortSigningKeyOut, operation_id="get_content_port_signing_key",
            summary="This site's content-port public key (for the importing site's trusted keys)")
def get_signing_key(db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> dict:
    return service.signing_key(db, user)


@router.post("/export", operation_id="export_proven_content", response_class=Response,
             summary="Export proven content as a signed bundle",
             responses={200: {"content": {"application/json": {}}, "description": "The signed bundle"},
                        409: {"description": "Something selected or needed is not proven"}})
def export(body: PortExportIn, db: Session = Depends(get_db),
           user: CurrentUser = Depends(_ADMIN)) -> Response:
    """The selected items and every item they need (a course's range templates and
    scenarios), each with its content, its proof summary and the golden-image builds it
    depends on. No Student data, credentials or tenant data. Audit-logged."""
    doc = service.export(db, user, body.items)
    db.commit()
    name = f"truenorth-content-{doc['bundle_id']}.json"
    return Response(json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/preview", response_model=PortPreviewOut, operation_id="preview_content_port_bundle",
             summary="Verify a bundle and show what importing it would do")
def preview(file: UploadFile = File(..., description="The signed content-port bundle (JSON)"),
            target_tenant_id: uuid.UUID | None = Form(None, description="Where it lands (default: your tenant)"),
            shared: bool = Form(False, description="Land as shared content every tenant reads"),
            db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> PortPreviewOut:
    """Nothing is stored except an audit row when the bundle is refused."""
    try:
        return service.preview(db, user, _raw(file), target_tenant_id=target_tenant_id, shared=shared)
    except HTTPException:
        db.commit()  # the refusal's audit row
        raise


@router.post("/import", response_model=PortImportOut, operation_id="import_content_port_bundle",
             summary="Import a verified bundle; every item lands not yet proven here")
def import_bundle(file: UploadFile = File(..., description="The signed content-port bundle (JSON)"),
                  target_tenant_id: uuid.UUID | None = Form(None, description="Where it lands (default: your tenant)"),
                  shared: bool = Form(False, description="Land as shared content every tenant reads"),
                  overwrite: list[str] = Form(default=[], description="Keys of conflicting items to overwrite"),
                  db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> PortImportOut:
    """A bundle is imported once. A conflict (changed here, a same-named item here, an older
    export, or a change between private and shared) is left as it is unless its key is in
    ``overwrite``. Audit-logged."""
    try:
        out = service.import_bundle(db, user, _raw(file), target_tenant_id=target_tenant_id, shared=shared,
                                    overwrite=[k.strip() for v in overwrite for k in v.split(",") if k.strip()])
        db.commit()
    except HTTPException:
        db.commit()  # refused before anything was written: only the refusal's audit row
        raise
    except IntegrityError:
        # The bundle id is unique: a concurrent import of the same bundle won (the flush of
        # its import row, or the commit). Nothing of this one is kept.
        db.rollback()
        raise HTTPException(409, "This bundle was imported meanwhile; nothing more was imported") from None
    return out
