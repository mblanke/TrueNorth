"""Publish accepted course releases to the tenant's Moodle (app/course_publishing).

Publishing is a job: the request records it (once per release and Moodle) and returns;
the job stages the course hidden, verifies it, then activates it. ``wait=true`` runs the
job inside the request instead, for scripts and tests that want the outcome directly.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..course_publishing import service
from ..course_publishing.models import CoursePublication
from ..course_publishing.runner import run_by_id
from ..course_releases.models import CourseRelease
from ..db import get_db
from ..models import ExternalPlatform
from ..rbac import Permission, require_permission
from ..tenancy import get_owned

router = APIRouter(tags=["course-publications"])


class PublishIn(BaseModel):
    platform_id: uuid.UUID


class CoursePublicationOut(BaseModel):
    id: uuid.UUID
    release_id: uuid.UUID
    course_id: uuid.UUID
    platform_id: uuid.UUID
    state: str
    error: str
    attempts: int
    receipt: dict[str, Any]
    created_at: datetime | None
    published_at: datetime | None


def _out(p: CoursePublication) -> CoursePublicationOut:
    return CoursePublicationOut(
        id=p.id,
        release_id=p.release_id,
        course_id=p.course_id,
        platform_id=p.platform_id,
        state=p.state,
        error=p.error,
        attempts=p.attempts,
        receipt=json.loads(p.receipt or "{}"),
        created_at=p.created_at,
        published_at=p.published_at,
    )


def _start(db: Session, pub: CoursePublication, wait: bool, background: BackgroundTasks) -> None:
    db.commit()
    if wait:
        try:
            service.run(db, pub)
        except service.PublishRefusedError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
    else:
        background.add_task(run_by_id, pub.id)


@router.post(
    "/course-releases/{release_id}/publications",
    response_model=CoursePublicationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def publish_course_release(
    release_id: uuid.UUID,
    body: PublishIn,
    background: BackgroundTasks,
    response: Response,
    wait: bool = Query(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_RELEASE)),
) -> CoursePublicationOut:
    """Publish the accepted release to one of the tenant's Moodles. Asking again for the
    same release and Moodle returns the existing job (and runs it again only if it failed)."""
    release = get_owned(db, CourseRelease, release_id, user)
    platform = get_owned(db, ExternalPlatform, body.platform_id, user)
    try:
        pub, created = service.request(db, release, platform, user_id=_uuid(user.id))
    except service.PublishRefusedError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if created or pub.state == "failed":
        if pub.state == "failed":
            service.retry(db, pub)
        _start(db, pub, wait, background)
    elif not created:
        response.status_code = status.HTTP_200_OK
    return _out(pub)


@router.get("/course-releases/{release_id}/publications", response_model=list[CoursePublicationOut])
def list_course_release_publications(
    release_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> list[CoursePublicationOut]:
    release = get_owned(db, CourseRelease, release_id, user)
    rows = db.query(CoursePublication).filter(CoursePublication.release_id == release.id).all()
    return [_out(p) for p in rows]


@router.get("/course-publications/{publication_id}", response_model=CoursePublicationOut)
def get_course_publication(
    publication_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> CoursePublicationOut:
    return _out(get_owned(db, CoursePublication, publication_id, user))


@router.post("/course-publications/{publication_id}/retry", response_model=CoursePublicationOut)
def retry_course_publication(
    publication_id: uuid.UUID,
    background: BackgroundTasks,
    wait: bool = Query(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_RELEASE)),
) -> CoursePublicationOut:
    """Run a failed or stalled publication again from staging (each step converges)."""
    pub = get_owned(db, CoursePublication, publication_id, user)
    if pub.state in service.RUNNING and pub.lease_until is not None:
        stalled_ids = {p.id for p in service.stalled(db)}
        if pub.id not in stalled_ids:
            raise HTTPException(409, "this publication is running")
    try:
        service.retry(db, pub)
    except service.PublishRefusedError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    _start(db, pub, wait, background)
    return _out(pub)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
