"""Course releases: upload an ARC² release candidate, accept it, download its parts.

The learner part is what a student may receive; the instructor part (rubric, solutions,
marking notes) is for ``course:author`` holders only and is never part of a learner
download. See ``app/course_releases`` for the lifecycle and verification rules.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..course_releases import bundle as bundle_mod
from ..course_releases import service
from ..course_releases.models import CourseRelease
from ..db import get_db
from ..models import Course
from ..rbac import Permission, require_permission
from ..tenancy import get_owned, tenant_uuid

logger = logging.getLogger("truenorth.api.course_releases")

router = APIRouter(prefix="/course-releases", tags=["course-releases"])


class OpenAction(BaseModel):
    id: str
    category: str
    text: str


class CourseReleaseOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    catalogue_code: str
    arc2_code: str
    title: str
    version: int
    state: str
    release_digest: str
    learner_digest: str
    platform_digest: str
    instructor_digest: str
    activities: dict[str, str]
    open_actions: list[OpenAction]
    acknowledged_actions: list[str]
    created_at: datetime | None
    accepted_at: datetime | None
    accepted_by: uuid.UUID | None
    notes: str


class AcceptIn(BaseModel):
    acknowledge_actions: list[str] = Field(default_factory=list)
    notes: str = ""


class CourseReleaseStatusOut(BaseModel):
    course_id: uuid.UUID
    legacy: bool
    active_release_id: uuid.UUID | None
    active_version: int | None
    candidates: int


def _out(r: CourseRelease) -> CourseReleaseOut:
    meta = json.loads(r.meta)
    return CourseReleaseOut(
        id=r.id,
        course_id=r.course_id,
        catalogue_code=r.catalogue_code,
        arc2_code=r.arc2_code,
        title=r.title,
        version=r.version,
        state=r.state,
        release_digest=r.release_digest,
        learner_digest=r.learner_digest,
        platform_digest=r.platform_digest,
        instructor_digest=r.instructor_digest,
        activities=meta.get("activities") or {},
        open_actions=[OpenAction(**a) for a in meta.get("open_human_actions") or []],
        acknowledged_actions=json.loads(r.acknowledged_actions or "[]"),
        created_at=r.created_at,
        accepted_at=r.accepted_at,
        accepted_by=r.accepted_by,
        notes=r.notes,
    )


def _refused(exc: service.ReleaseRefusedError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post("", response_model=CourseReleaseOut, status_code=status.HTTP_201_CREATED)
async def upload_course_release(
    file: UploadFile,
    response: Response,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> CourseReleaseOut:
    """Upload an ARC² release tarball (``python -m arc2.release build``) as a candidate.
    Re-uploading the same release returns the existing candidate with 200."""
    data = await file.read(bundle_mod.MAX_BYTES + 1)
    try:
        release, created = service.create_candidate(db, data, tenant_id=tenant_uuid(user), user_id=_user_uuid(user))
    except service.ReleaseRefusedError as exc:  # refused before anything is written
        raise _refused(exc) from exc
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return _out(release)


@router.get("", response_model=list[CourseReleaseOut])
def list_course_releases(
    course_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> list[CourseReleaseOut]:
    q = db.query(CourseRelease).filter(CourseRelease.tenant_id == tenant_uuid(user))
    if course_id is not None:
        q = q.filter(CourseRelease.course_id == course_id)
    return [_out(r) for r in q.order_by(CourseRelease.catalogue_code, CourseRelease.version.desc()).all()]


@router.get("/courses/{course_id}", response_model=CourseReleaseStatusOut)
def get_course_release_status(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> CourseReleaseStatusOut:
    """Which release a course delivers; ``legacy`` when it has none (content that predates
    releases)."""
    course = get_owned(db, Course, course_id, user)
    return CourseReleaseStatusOut(**service.course_status(db, course))


@router.get("/{release_id}", response_model=CourseReleaseOut)
def get_course_release(
    release_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> CourseReleaseOut:
    return _out(get_owned(db, CourseRelease, release_id, user))


@router.post("/{release_id}/accept", response_model=CourseReleaseOut)
def accept_course_release(
    release_id: uuid.UUID,
    body: AcceptIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_RELEASE)),
) -> CourseReleaseOut:
    """Accept a candidate: its content becomes the course's, the previous release is
    superseded, and enrollments made from now on pin to it. Open ARC² actions must each be
    acknowledged by id."""
    release = get_owned(db, CourseRelease, release_id, user)
    try:
        service.accept(db, release, user_id=_user_uuid(user), acknowledge=body.acknowledge_actions, notes=body.notes)
    except service.ReleaseRefusedError as exc:  # refused before anything is written
        raise _refused(exc) from exc
    except ValueError as exc:  # the importer refusing the content
        db.rollback()
        raise HTTPException(422, f"release content could not be imported: {exc}") from exc
    db.commit()
    return _out(release)


def _part(db: Session, user: CurrentUser, release_id: uuid.UUID, part: str) -> Response:
    release = get_owned(db, CourseRelease, release_id, user)
    data = bundle_mod.part_tarball(service.load_bundle(db, release), part)
    code = re.sub(r"[^A-Za-z0-9_.-]", "_", release.catalogue_code)
    name = f"{code}-v{release.version}-{part}.tar.gz"
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/{release_id}/learner-bundle", response_class=Response)
def get_course_release_learner_bundle(
    release_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> Response:
    return _part(db, user, release_id, "learner")


@router.get("/{release_id}/instructor-bundle", response_class=Response)
def get_course_release_instructor_bundle(
    release_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> Response:
    """The instructor pack: rubric, solutions, marking notes. Never a student download."""
    return _part(db, user, release_id, "instructor")


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError, AttributeError):
        return None
