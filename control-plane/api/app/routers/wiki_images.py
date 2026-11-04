"""TrueNorth Range — images in wiki pages (issue #379).

==================================================  ==========================
Endpoint                                            Permission(s)
==================================================  ==========================
GET    /wiki/pages/{page_id}/images                 WIKI_EDIT (the page's images, to manage them)
POST   /wiki/pages/{page_id}/images                 WIKI_EDIT
GET    /wiki/images/{image_id}                      WIKI_READ (+ the page is visible to you)
DELETE /wiki/images/{image_id}                      WIKI_EDIT
==================================================  ==========================

An image belongs to a page, and whoever can see the page can see its image; the id alone
grants nothing (a staff-only space, an unpublished page, an archived space, a deleted page
and another tenant are all a 404). The bytes are checked and cleaned by app/wiki_images.py
before they are stored: type from the bytes, no SVG, a pixel cap, Exif and text metadata
removed. They are served with their checked type, ``nosniff`` and a sandboxing CSP, so even a
file that slipped through cannot run. Pages reference an image as
``![alt](/api/wiki/images/<id>)``; the web viewer fetches it with the user's token.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import object_store, wiki_images
from ..auth import CurrentUser
from ..db import get_db
from ..models_wiki import WikiImage
from ..rbac import Permission, require_permission
from ..tenancy import get_owned
from . import wiki

logger = logging.getLogger("truenorth.api.wiki_images")

router = APIRouter(prefix="/wiki", tags=["wiki"])

WIKI_BUCKET = "wiki"
MAX_IMAGES_PER_PAGE = 100


class WikiImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    width: int
    height: int
    # What to put in the page: ![alt](url)
    url: str = ""


def _out(img: WikiImage) -> WikiImageOut:
    out = WikiImageOut.model_validate(img)
    out.url = f"/api/wiki/images/{img.id}"
    return out


def _filename(raw: str | None, kind: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", (raw or "image").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    stem = stem.strip(" .") or "image"
    return f"{stem[:200]}"[:250]


@router.get("/pages/{page_id}/images", response_model=list[WikiImageOut])
def list_images(
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> list[WikiImageOut]:
    """Every image uploaded to the page, so an editor can see and remove what it holds.

    Removing the markdown that shows an image does not remove the image (older revisions still
    point at it); removing it here does."""
    page, _space = wiki._page(db, page_id, user)
    rows = (
        db.query(WikiImage)
        .filter(WikiImage.page_id == page.id, WikiImage.tenant_id == page.tenant_id, WikiImage.deleted_at.is_(None))
        .order_by(WikiImage.created_at, WikiImage.id)
        .all()
    )
    return [_out(i) for i in rows]


@router.post("/pages/{page_id}/images", response_model=WikiImageOut, status_code=201)
async def upload_image(
    file: UploadFile,
    page_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> WikiImageOut:
    page, space = wiki._page(db, page_id, user)
    wiki._writable(space)
    if space.system_owned:
        raise HTTPException(409, "Generated pages cannot hold uploaded images.")
    data = await file.read(wiki_images.MAX_BYTES + 1)  # never more than the limit and one byte
    try:
        checked = wiki_images.check(data)
    except wiki_images.ImageError as exc:
        raise HTTPException(422, str(exc)) from exc
    count = (
        db.query(WikiImage)
        .filter(WikiImage.page_id == page.id, WikiImage.tenant_id == page.tenant_id, WikiImage.deleted_at.is_(None))
        .count()
    )
    if count >= MAX_IMAGES_PER_PAGE:
        raise HTTPException(409, f"A page can hold at most {MAX_IMAGES_PER_PAGE} images.")
    img = WikiImage(
        tenant_id=page.tenant_id,
        page_id=page.id,
        filename=_filename(file.filename, checked.kind),
        content_type=checked.content_type,
        size_bytes=len(checked.data),
        width=checked.width,
        height=checked.height,
        uploaded_by=uuid.UUID(user.id),
    )
    db.add(img)
    db.flush()
    key = img.object_key = f"{page.tenant_id}/{page.id}/{img.id}.{wiki_images.EXTENSIONS[checked.kind]}"
    try:
        await run_in_threadpool(object_store.put_object, key, checked.data, img.content_type, WIKI_BUCKET)
        wiki._audit(db, user, "wiki_image_upload", "wiki_image", str(img.id))
        db.commit()
    except Exception:
        db.rollback()
        with contextlib.suppress(Exception):
            object_store.delete_object(key, bucket=WIKI_BUCKET)  # `key` was read before the rollback expired img
        raise
    db.refresh(img)
    return _out(img)


def _image(db: Session, image_id: uuid.UUID, user: CurrentUser) -> WikiImage:
    img = get_owned(db, WikiImage, image_id, user, not_found="Image not found")
    try:
        wiki._page(db, img.page_id, user)  # the page's visibility is the image's
    except HTTPException:
        raise HTTPException(404, "Image not found") from None
    return img


@router.get("/images/{image_id}")
def get_image(
    image_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_READ)),
) -> Response:
    img = _image(db, image_id, user)
    if img.content_type not in wiki_images.TYPES.values():  # only ever what check() produced
        raise HTTPException(404, "Image not found")
    try:
        data = object_store.get_object(img.object_key, bucket=WIKI_BUCKET)
    except Exception as exc:  # noqa: BLE001 — object store faults are a 502, not a crash
        logger.error("Could not read %s from object storage: %s", img.object_key, exc)
        raise HTTPException(502, "Image storage is unavailable") from exc
    return Response(
        content=data,
        media_type=img.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=300",
            "Vary": "Authorization",
            "Content-Disposition": "inline",
        },
    )


@router.delete("/images/{image_id}", status_code=204, response_class=Response)
def delete_image(
    image_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.WIKI_EDIT)),
) -> Response:
    img = _image(db, image_id, user)
    _page, space = wiki._page(db, img.page_id, user)  # tenant-scoped; _image already checked it is visible
    wiki._writable(space)
    img.soft_delete()
    wiki._audit(db, user, "wiki_image_delete", "wiki_image", str(img.id))
    key = img.object_key
    db.commit()
    with contextlib.suppress(Exception):
        object_store.delete_object(key, bucket=WIKI_BUCKET)
    return Response(status_code=204)


__all__ = ["router"]
