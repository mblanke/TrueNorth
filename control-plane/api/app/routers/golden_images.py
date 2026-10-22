"""Golden-image / template-library router.

Import the golden-image catalogue, browse the registry, resolve an OS alias to a
hypervisor template, let an operator register the real template name / datastore
and build status per image, and register custom variant images (POST).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import golden_images
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..golden_image_builds import service as builds
from ..golden_image_builds.models import GoldenImageBuild
from ..golden_image_builds.schemas import GoldenImageBuildIn, GoldenImageBuildOut
from ..models import GoldenImage
from ..rbac import Permission, is_platform_admin, require_permission

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
    # The build every new range clones (ADR 0013 §2); None until an admin promotes one.
    promoted_build: GoldenImageBuildOut | None = None


class GoldenImageCreate(BaseModel):
    """A custom (non-catalogue) image, e.g. a Packer variant from infra/vsphere/packer/variants/."""

    catalogue_id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    os_family: Literal["windows", "linux", "appliance"]
    version: str = Field("", max_length=60)
    role: str = Field("", max_length=160)
    hypervisor: Literal["vsphere", "proxmox", "hyperv"] = "vsphere"
    template_name: str = Field("", max_length=120)
    datastore: str | None = Field(None, max_length=120)
    os_aliases: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]] = Field(
        default_factory=list, max_length=32
    )
    build_status: Literal["planned", "building", "built", "failed"] = "planned"
    enabled: bool = True
    golden_gb: int | None = Field(None, ge=0, le=4096)
    notes: str = Field("", max_length=1000)


class GoldenImagePatch(BaseModel):
    template_name: str | None = None
    datastore: str | None = None
    enabled: bool | None = None
    build_status: str | None = None
    checksum: str | None = None
    # A build the template kit registers (ADR 0013 §2). Recorded, never promoted, here.
    build: GoldenImageBuildIn | None = None


def _promoted(db: Session, images: list[GoldenImage]) -> dict:
    ids = [i.id for i in images]
    if not ids:
        return {}
    rows = db.query(GoldenImageBuild).filter(GoldenImageBuild.golden_image_id.in_(ids),
                                             GoldenImageBuild.promoted.is_(True)).all()
    return {r.golden_image_id: builds.build_out(r) for r in rows}


def _out(img: GoldenImage, promoted: dict | None = None) -> GoldenImageOut:
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
        promoted_build=(promoted or {}).get(img.id),
    )


@router.post("/import-catalogue")
async def import_catalogue(
    file: UploadFile,
    hypervisor: str = Query("vsphere"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INFRA_WRITE)),
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


@router.post("", response_model=GoldenImageOut)
def upsert_custom_image(
    body: GoldenImageCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INFRA_WRITE)),
) -> GoldenImageOut:
    """Register a custom image (a Packer variant) so ranges and the designer can use it.

    Creates (201) or updates (200) the image keyed on (catalogue_id, hypervisor). It is
    marked as a variant, so catalogue re-imports leave it alone. A catalogue image's
    slot is refused with 409; change those through PATCH.

    **Permission: infra:write**: the registry is platform-wide, like PATCH below.
    """
    # tenant-safe: platform-wide registry keyed on (catalogue_id, hypervisor), same as
    # PATCH; tenant_id only records who registered it.
    try:
        img, created = golden_images.upsert_variant(
            db, body.model_dump(), tenant_id=user.tenant_id or None
        )
    except golden_images.CatalogueConflictError as exc:
        # Raised before anything is written, so there is nothing to roll back.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return _out(img, _promoted(db, [img]))


@router.get("", response_model=list[GoldenImageOut])
def list_images(
    hypervisor: str | None = Query(None),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> list[GoldenImageOut]:
    q = db.query(GoldenImage).filter(GoldenImage.deleted_at.is_(None))
    if hypervisor:
        q = q.filter(GoldenImage.hypervisor == hypervisor)
    images = q.order_by(GoldenImage.catalogue_id).all()
    promoted = _promoted(db, images)
    return [_out(i, promoted) for i in images]


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
    user: CurrentUser = Depends(require_permission(Permission.INFRA_WRITE)),
) -> GoldenImageOut:
    """Operator registration: set the real template name / datastore / enabled / build status,
    and record a build (``build``: id, checksum, build date, ISO checksum, per-host copies).

    A recorded build is not used by ranges until an admin promotes it
    (POST /golden-images/{image_id}/builds/{build_id}/promote, ADR 0013 §2).

    **Permission: infra:write** — the registry is platform-wide, so only operators edit it.
    A ``build`` needs the platform administrator (403 otherwise).
    """
    # tenant-safe: the golden-image registry is platform infrastructure, one row per
    # (catalogue_id, hypervisor) for the whole install (uq_image_hypervisor), shared by
    # every tenant's ranges; tenant_id only records who imported it. Writes are gated
    # on infra:write (admin, range_ops) above instead of on tenant.
    img = db.query(GoldenImage).filter_by(id=image_id).one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="golden image not found")
    if body.build is not None:  # checked before anything is changed: a conflict writes nothing
        if not is_platform_admin(user, db):  # one record serves every tenant's ranges (ADR 0013 §2)
            raise HTTPException(status_code=403, detail="Only a platform administrator can record a golden-image "
                                "build: it applies to every tenant")
        try:
            builds.record_build(db, img, body.build, user, template_name=body.template_name)
        except builds.BuildConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    for field in ("template_name", "datastore", "enabled", "build_status", "checksum"):
        val = getattr(body, field)
        if val is not None:
            setattr(img, field, val)
    db.commit()
    db.refresh(img)
    return _out(img, _promoted(db, [img]))
