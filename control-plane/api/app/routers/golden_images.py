"""Golden-image / template-library router.

Import the golden-image catalogue, browse the registry, resolve an OS alias to a
hypervisor template, and let an operator register the real template name / datastore
and build status per image.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import golden_images
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import GoldenImage

logger = logging.getLogger("truenorth.api.golden_images")

router = APIRouter(prefix="/golden-images", tags=["golden-images"])

MAX_CSV_BYTES = 4 * 1024 * 1024


class GoldenImageOut(BaseModel):
    id: str
    catalogue_id: str
    os_family: str
    version: str
    role: str
    hypervisor: str
    template_name: str
    datastore: str
    os_aliases: list[str]
    sensor_baked: bool
    enabled: bool
    build_status: str
    golden_gb: int
    notes: str


class GoldenImagePatch(BaseModel):
    template_name: str | None = None
    datastore: str | None = None
    enabled: bool | None = None
    build_status: str | None = None
    checksum: str | None = None


def _out(img: GoldenImage) -> GoldenImageOut:
    try:
        aliases = json.loads(img.os_aliases or "[]")
    except json.JSONDecodeError:
        aliases = []
    return GoldenImageOut(
        id=str(img.id),
        catalogue_id=img.catalogue_id,
        os_family=img.os_family,
        version=img.version,
        role=img.role,
        hypervisor=img.hypervisor,
        template_name=img.template_name,
        datastore=img.datastore,
        os_aliases=aliases,
        sensor_baked=img.sensor_baked,
        enabled=img.enabled,
        build_status=img.build_status,
        golden_gb=img.golden_gb,
        notes=img.notes,
    )


@router.post("/import-catalogue")
async def import_catalogue(
    file: UploadFile,
    hypervisor: str = Query("vsphere"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Seed the golden-image registry from vm_iso_catalogue.csv (idempotent)."""
    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="catalogue too large")
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"catalogue must be UTF-8 CSV: {exc}") from exc
    try:
        stats = golden_images.import_catalogue(db, csv_text, hypervisor=hypervisor, tenant_id=user.tenant_id or None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("golden-image import failed")
        raise HTTPException(status_code=422, detail=f"import failed: {exc}") from exc
    return {"imported": True, **stats}


@router.get("", response_model=list[GoldenImageOut])
def list_images(
    hypervisor: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> list[GoldenImageOut]:
    q = db.query(GoldenImage).filter(GoldenImage.deleted_at.is_(None))
    if hypervisor:
        q = q.filter(GoldenImage.hypervisor == hypervisor)
    return [_out(i) for i in q.order_by(GoldenImage.catalogue_id).all()]


@router.get("/resolve")
def resolve(
    os: str = Query(..., description="topology OS alias, e.g. windows-server-2019"),
    hypervisor: str = Query("vsphere"),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    tn = golden_images.resolve_template(db, os, hypervisor)
    if tn is None:
        raise HTTPException(status_code=404, detail=f"no enabled golden image maps '{os}' on {hypervisor}")
    return {"os": os, "hypervisor": hypervisor, "template_name": tn}


@router.get("/alias-map")
def alias_map(
    hypervisor: str = Query("vsphere"),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    return {"hypervisor": hypervisor, "map": golden_images.resolve_map(db, hypervisor)}


@router.patch("/{image_id}", response_model=GoldenImageOut)
def update_image(
    image_id: str,
    body: GoldenImagePatch,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> GoldenImageOut:
    """Operator registration: set the real template name / datastore / enabled / build status."""
    img = db.query(GoldenImage).filter_by(id=image_id).one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="golden image not found")
    for field in ("template_name", "datastore", "enabled", "build_status", "checksum"):
        val = getattr(body, field)
        if val is not None:
            setattr(img, field, val)
    db.commit()
    db.refresh(img)
    return _out(img)
