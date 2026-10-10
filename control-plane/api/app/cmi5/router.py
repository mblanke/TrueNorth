"""cmi5 routes: the served course structure, the AU runtime's content, and TrueNorth's LMS
side (launch, fetch, the AU's LRS, abandon, waive). docs/cmi5.md.

Authentication, route by route:

* structure, content, grade, launch, abandon, waive, cmi5.xml: a signed-in TrueNorth user,
  scoped to their tenant (``get_owned``); unreleased candidates are for course authors only;
  launch also needs an enrolment pinned to the release.
* ``POST /cmi5/fetch/{secret}``: the one-time fetch secret in the path is the credential
  (``cmi5_fetch_credential``). It is single-use, expires 15 minutes after launch, is stored
  hashed, and is never logged (app/middleware.py, both nginx configs).
* ``/cmi5/lrs/...``: the session's auth token as Basic credentials
  (``cmi5_session_credential``), good for that session's actor, registration and activity
  only, until it terminates, is abandoned or expires.

Neither token-authenticated route uses a browser cookie, so CSRF does not apply to them
(``CSRF_EXEMPT_PREFIXES``).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..course_releases.models import CANDIDATE, CourseRelease
from ..course_releases.service import pinned_release
from ..db import get_db
from ..models import Course, Enrollment
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import get_owned
from . import content as content_mod
from . import lms
from . import structure as structure_mod
from .models import Cmi5Grade, Cmi5Registration, Cmi5Session
from .rules import RuleViolationError
from .schemas import (
    Cmi5AuOut,
    Cmi5ContentOut,
    Cmi5FetchOut,
    Cmi5GradeIn,
    Cmi5GradeOut,
    Cmi5LaunchIn,
    Cmi5LaunchOut,
    Cmi5SatisfiedOut,
    Cmi5StructureOut,
    Cmi5WaiveIn,
)

router = APIRouter(prefix="/cmi5", tags=["cmi5"])
XAPI_VERSION = {"X-Experience-API-Version": "1.0.3"}


def _http(exc: lms.Cmi5Error | content_mod.ContentError) -> HTTPException:
    if isinstance(exc, content_mod.ContentError):
        return HTTPException(422, str(exc))
    return HTTPException(exc.status, exc.message)


def _release(db: Session, release_id: uuid.UUID, user: CurrentUser) -> CourseRelease:
    """The caller's tenant's release. Without course:author, only an accepted (or superseded)
    release of a published course; anything else is 404, as the course catalogue treats a
    draft (routers/courses.py)."""
    release = get_owned(db, CourseRelease, release_id, user, not_found="release not found")
    if user_has_permission(user, Permission.COURSE_AUTHOR):
        return release
    course = db.get(Course, release.course_id)  # tenant-safe: the owned release's own course
    if release.state == CANDIDATE or course is None or not course.is_published:
        raise HTTPException(404, "release not found")
    return release


def _enrolled(db: Session, release: CourseRelease, user: CurrentUser) -> Enrollment | None:
    """A Student reads and marks a module only on their own enrolment in this release, the
    same rule as launching it; course authors (who hold the answer key anyway) need none."""
    if user_has_permission(user, Permission.COURSE_AUTHOR):
        return None
    try:
        return lms.enrolment_for(db, uuid.UUID(user.id), release)
    except lms.Cmi5Error as exc:
        raise _http(exc) from exc


def _serialise_marking(db: Session, enrolled: Enrollment) -> None:
    """Take the enrolment's row lock before counting a Student's marks, so two concurrent
    submissions cannot both see room under the limit. A no-op UPDATE is the portable form:
    a row lock on PostgreSQL, the database write lock on SQLite (where FOR UPDATE is ignored)."""
    db.query(Enrollment).filter(Enrollment.id == enrolled.id, Enrollment.user_id == enrolled.user_id).update(
        {Enrollment.id: Enrollment.id}, synchronize_session=False
    )


def _grade_attempts() -> int:
    try:
        return max(int(os.getenv("CMI5_GRADE_ATTEMPTS", "3")), 1)
    except ValueError:
        return 3


def _package(db: Session, release: CourseRelease):
    try:
        return content_mod.package(db, release)
    except content_mod.ContentError as exc:
        raise _http(exc) from exc


def _text(langs: dict[str, str]) -> str:
    return next(iter(langs.values()), "")


@router.get("/releases/{release_id}/cmi5.xml", response_class=Response)
def get_cmi5_course_structure(
    release_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
):
    """The release's cmi5 course structure for another LMS (PCTE, Moodle): every AU URL
    absolute (TrueNorth's AU runtime), ``launchMethod="OwnWindow"``."""
    release = _release(db, release_id, user)
    bundle, _ = _package(db, release)
    data = bundle.files["learner"][structure_mod.STRUCTURE_PATH]
    return Response(
        content=structure_mod.served_xml(data, release.id),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{release.slug}-v{release.version}-cmi5.xml"'},
    )


@router.get("/releases/{release_id}/structure", response_model=Cmi5StructureOut)
def get_cmi5_structure(
    release_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """The AUs of a release, with the caller's own progress when they have a registration."""
    release = _release(db, release_id, user)
    _, parsed = _package(db, release)
    me = uuid.UUID(user.id)
    enrolled = db.query(Enrollment).filter(Enrollment.user_id == me, Enrollment.course_id == release.course_id).first()
    pinned = pinned_release(db, enrolled.id) if enrolled else None
    reg = db.get(Cmi5Registration, enrolled.id) if enrolled else None  # tenant-safe: the caller's own enrolment
    data = lms.progress(reg) if reg and reg.release_id == release.id else {"aus": {}, "satisfied": []}
    aus = []
    for au in parsed.aus:
        state = data["aus"].get(str(au.index)) or {}
        aus.append(
            Cmi5AuOut(
                index=au.index,
                publisher_id=au.publisher_id,
                title=_text(au.title),
                description=_text(au.description),
                move_on=au.move_on,
                mastery_score=au.mastery_score,
                url=structure_mod.au_url(release.id, au.index),
                completed=bool(state.get("completed")),
                passed=bool(state.get("passed")),
                waived=state.get("waived"),
                satisfied=lms.au_satisfied(au, data["aus"]) if reg else False,
            )
        )
    return Cmi5StructureOut(
        release_id=release.id,
        course_id=release.course_id,
        publisher_id=parsed.publisher_id,
        title=_text(parsed.title) or release.title,
        registration=reg.id if reg and reg.release_id == release.id else None,
        course_satisfied="course" in data["satisfied"],
        enrolled=bool(pinned and pinned.id == release.id),
        aus=aus,
    )


@router.get("/releases/{release_id}/aus/{au_index}/content", response_model=Cmi5ContentOut)
def get_cmi5_au_content(
    release_id: uuid.UUID,
    au_index: int = Path(..., ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Pages and quiz questions for TrueNorth's AU runtime. Never the answers."""
    release = _release(db, release_id, user)
    _enrolled(db, release, user)
    bundle, parsed = _package(db, release)
    try:
        return content_mod.au_content(bundle, parsed, au_index)
    except content_mod.ContentError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/releases/{release_id}/aus/{au_index}/grade", response_model=Cmi5GradeOut)
def grade_cmi5_au_quiz(
    body: Cmi5GradeIn,
    release_id: uuid.UUID,
    au_index: int = Path(..., ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Mark the AU's quiz on the server, record the mark, and return only the totals (no
    per-question result). A Student needs an enrolment on the release and gets
    CMI5_GRADE_ATTEMPTS marks per AU per 24 hours (default 3). In a TrueNorth session the
    AU's passed/failed must then report this mark (TN-GRADE); an AU launched by another LMS
    reports to that LMS, which TrueNorth cannot vouch for (docs/cmi5.md)."""
    release = _release(db, release_id, user)
    enrolled = _enrolled(db, release, user)
    me = uuid.UUID(user.id)
    now = datetime.now(UTC)
    if enrolled is not None:
        _serialise_marking(db, enrolled)
        used = (
            db.query(Cmi5Grade)
            .filter(
                Cmi5Grade.user_id == me,
                Cmi5Grade.release_id == release.id,
                Cmi5Grade.au_index == au_index,
                Cmi5Grade.created_at >= now - timedelta(hours=24),
            )
            .count()
        )
        if used >= _grade_attempts():
            raise HTTPException(429, f"{used} attempts at this quiz in the last 24 hours; try again later")
    bundle, parsed = _package(db, release)
    try:
        marked = content_mod.grade(bundle, parsed, au_index, body.answers)
    except content_mod.ContentError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.add(
        Cmi5Grade(
            tenant_id=release.tenant_id,
            user_id=me,
            release_id=release.id,
            au_index=au_index,
            correct=marked["correct"],
            total=marked["total"],
            scaled=marked["scaled"],
            created_at=now,
        )
    )
    db.commit()
    return marked


@router.post("/releases/{release_id}/aus/{au_index}/launch", response_model=Cmi5LaunchOut)
def launch_cmi5_au(
    body: Cmi5LaunchIn,
    release_id: uuid.UUID,
    au_index: int = Path(..., ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Launch an AU for the caller (TrueNorth as the cmi5 LMS): records ``launched`` and
    ``LMS.LaunchData`` in the LRS and returns the AU URL with the cmi5 launch parameters."""
    release = _release(db, release_id, user)
    try:
        out = lms.launch(db, uuid.UUID(user.id), release, au_index, body.launch_mode)
    except (lms.Cmi5Error, content_mod.ContentError) as exc:
        # What happened before the failure is already in the LRS (an abandoned session, a
        # registration's NotApplicable satisfaction); keep the database in step with it.
        # No session was created: that is the launch's last step.
        db.commit()
        raise _http(exc) from exc
    db.commit()
    return Cmi5LaunchOut(**out.__dict__)


def cmi5_fetch_credential(secret: str = Path(..., min_length=20, max_length=128)) -> str:
    """The fetch URL's one-time secret is this route's credential (checked in ``lms.fetch``)."""
    return secret


@router.post(
    "/fetch/{secret}",
    response_model=Cmi5FetchOut,
    response_model_exclude_none=True,
    response_model_by_alias=True,
)
def fetch_cmi5_auth_token(secret: str = Depends(cmi5_fetch_credential), db: Session = Depends(get_db)):
    """cmi5 fetch URL: the session's auth token, once. Errors are HTTP 200 with
    ``error-code`` 1 (already returned or expired) or 2 (unknown), as cmi5 8.2 requires."""
    out = lms.fetch(db, secret)
    db.commit()
    return Cmi5FetchOut.model_validate(out)


def cmi5_session_credential(
    db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> Cmi5Session:
    """The session behind the Basic auth token; 401/403 when it is not a live session."""
    try:
        return lms.authenticate(db, authorization)
    except lms.Cmi5Error as exc:  # nothing was written
        raise HTTPException(exc.status, exc.message, headers=XAPI_VERSION) from exc


async def _proxied(request: Request, resource: str, session: Cmi5Session, db: Session) -> Response:
    params = dict(request.query_params)
    body = await request.body()
    try:
        resp = lms.proxy(db, session, request.method, resource, params, body, dict(request.headers))
    except RuleViolationError as v:  # refused before anything was written or forwarded
        return JSONResponse({"error": v.message, "violatedReqId": v.req}, status_code=v.status, headers=XAPI_VERSION)
    except (lms.Cmi5Error, content_mod.ContentError) as exc:  # the LRS failed before any write
        err = _http(exc)
        return JSONResponse({"error": err.detail}, status_code=err.status_code, headers=XAPI_VERSION)
    db.commit()
    headers = {**XAPI_VERSION, **{k: v for k, v in resp.headers.items() if k in ("etag", "last-modified")}}
    return Response(
        content=resp.body,
        status_code=resp.status,
        media_type=resp.headers.get("content-type"),
        headers=headers,
    )


@router.get("/lrs/{resource:path}", include_in_schema=True)
async def cmi5_lrs_get(
    request: Request,
    resource: str,
    session: Cmi5Session = Depends(cmi5_session_credential),
    db: Session = Depends(get_db),
):
    """The AU's xAPI endpoint (read): LMS.LaunchData, learner preferences, its own State."""
    return await _proxied(request, resource, session, db)


@router.post("/lrs/{resource:path}")
async def cmi5_lrs_post(
    request: Request,
    resource: str,
    session: Cmi5Session = Depends(cmi5_session_credential),
    db: Session = Depends(get_db),
):
    """The AU's xAPI endpoint (statements, State documents), checked against the cmi5 rules."""
    return await _proxied(request, resource, session, db)


@router.put("/lrs/{resource:path}")
async def cmi5_lrs_put(
    request: Request,
    resource: str,
    session: Cmi5Session = Depends(cmi5_session_credential),
    db: Session = Depends(get_db),
):
    """The AU's xAPI endpoint (a statement by id, State documents)."""
    return await _proxied(request, resource, session, db)


@router.delete("/lrs/{resource:path}")
async def cmi5_lrs_delete(
    request: Request,
    resource: str,
    session: Cmi5Session = Depends(cmi5_session_credential),
    db: Session = Depends(get_db),
):
    """The AU's xAPI endpoint (its own State documents)."""
    return await _proxied(request, resource, session, db)


@router.post("/sessions/{session_id}/abandon", status_code=204)
def abandon_cmi5_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """End an open session abnormally (``abandoned``): its Student, or a records holder in the tenant."""
    session = get_owned(db, Cmi5Session, session_id, user, not_found="session not found")
    if str(session.user_id) != user.id and not user_has_permission(user, Permission.LEARNING_RECORD_WRITE):
        raise HTTPException(404, "session not found")
    reg = db.get(Cmi5Registration, session.registration_id)  # tenant-safe: the owned session's registration
    release = db.get(CourseRelease, reg.release_id)  # tenant-safe: that registration's release
    try:
        lms.abandon(db, session, release)
    except (lms.Cmi5Error, content_mod.ContentError) as exc:  # the LRS refused; nothing changed
        raise _http(exc) from exc
    db.commit()
    return Response(status_code=204)


@router.post("/registrations/{registration_id}/aus/{au_index}/waive", response_model=Cmi5SatisfiedOut)
def waive_cmi5_au(
    body: Cmi5WaiveIn,
    registration_id: uuid.UUID,
    au_index: int = Path(..., ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.LEARNING_RECORD_WRITE)),
):
    """Waive an AU for a Student's registration (``waived`` with the reason), then moveOn."""
    reg = get_owned(db, Cmi5Registration, registration_id, user, not_found="registration not found")
    release = db.get(CourseRelease, reg.release_id)  # tenant-safe: the owned registration's release
    try:
        newly = lms.waive(db, reg, release, au_index, body.reason)
    except (lms.Cmi5Error, content_mod.ContentError) as exc:  # the LRS refused; nothing changed
        raise _http(exc) from exc
    db.commit()
    return Cmi5SatisfiedOut(satisfied=newly)
