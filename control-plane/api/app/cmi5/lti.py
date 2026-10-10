"""cmi5 AUs as LTI 1.3 resources: an LMS (Moodle) launches TrueNorth's AU with TrueNorth as
the cmi5 LMS, and gets the result in its gradebook (ags.py). docs/cmi5.md, "Moodle".

Resource ``cmi5:<release id>:<AU index>``, from a deep-linking content item
(:func:`picker_items`, :func:`content_item`). A launch (:func:`launch_target`, called by
``routers/integrations.py`` before it records the launch) is refused unless:

* the release is the platform's tenant's, accepted (or superseded), of a published course,
  and has that AU (404 otherwise, as for another tenant's exercise);
* the launched account is in the platform's tenant (404);
* the account is enrolled in the course (403) on this release (409), and the enrolment is
  not withdrawn (403). The launch does not enrol: a ``course`` launch never has, and
  enrolment stays TrueNorth's (the ``lab`` launch's auto-enrolment is not extended here);
* cmi5 is configured (``CMI5_LRS_AUTH``; 503), as for any cmi5 launch.

It then finds or creates the enrolment's cmi5 registration and sends the browser to
``/au/releases/<release>?launch=<n>&lti=1``, where the SPA launches the AU for the Student
(``POST /cmi5/releases/{id}/aus/{n}/launch``).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from sqlalchemy.orm import Session

from ..course_releases.models import ACCEPTED, SUPERSEDED, CourseRelease
from ..models import Course, EnrollmentStatus, User
from . import content as content_mod
from . import lms
from .ags import RESOURCE_KIND, resource_id

PICKER_RELEASES = 20  # releases with cmi5 modules listed in the deep-linking picker
PICKER_SCAN = 200  # accepted releases looked at to find them

# AU titles per release blob (sha256; bundles are immutable), None for a release with no
# usable cmi5 package: the picker parses each bundle once per process, not once per view.
_TITLES: OrderedDict[str, tuple[str, ...] | None] = OrderedDict()
_TITLES_SIZE = 512
_TITLES_LOCK = threading.Lock()


def _au_titles(db: Session, release: CourseRelease) -> tuple[str, ...] | None:
    with _TITLES_LOCK:
        if release.blob_sha256 in _TITLES:
            _TITLES.move_to_end(release.blob_sha256)
            return _TITLES[release.blob_sha256]
    try:
        _, parsed = content_mod.package(db, release)
        titles: tuple[str, ...] | None = tuple(
            next(iter(au.title.values()), "") or f"Module {au.index + 1}" for au in parsed.aus
        )
    except content_mod.ContentError:
        titles = None
    with _TITLES_LOCK:
        _TITLES[release.blob_sha256] = titles
        while len(_TITLES) > _TITLES_SIZE:
            _TITLES.popitem(last=False)
    return titles


def _parse(rid: str) -> tuple[uuid.UUID, int]:
    release_part, _, index_part = (rid or "").partition(":")
    try:
        release_id = uuid.UUID(release_part)
        au_index = int(index_part)
    except ValueError as exc:
        raise lms.Cmi5Error(400, "malformed cmi5 link: expected <release id>:<AU index>") from exc
    if au_index < 0 or str(au_index) != index_part:
        raise lms.Cmi5Error(400, "malformed cmi5 link: expected <release id>:<AU index>")
    return release_id, au_index


def _release(db: Session, tenant_id: uuid.UUID, release_id: uuid.UUID) -> CourseRelease:
    """A release a Student of this tenant may be sent to, or 404."""
    release = (
        db.query(CourseRelease)
        .filter(CourseRelease.id == release_id, CourseRelease.tenant_id == tenant_id)
        .one_or_none()
    )
    if release is None or release.state not in (ACCEPTED, SUPERSEDED):
        raise lms.Cmi5Error(404, "course module not found")
    course = db.get(Course, release.course_id)  # tenant-safe: the tenant's release's own course
    if course is None or not course.is_published:
        raise lms.Cmi5Error(404, "course module not found")
    return release


def _au_title(db: Session, release: CourseRelease, au_index: int) -> str:
    titles = _au_titles(db, release)
    if titles is None or not 0 <= au_index < len(titles):
        raise lms.Cmi5Error(404, "course module not found")
    return titles[au_index]


def spa_path(rid: str) -> str:
    """Where the browser goes for a checked resource id."""
    release_id, au_index = _parse(rid)
    return f"/au/releases/{release_id}?launch={au_index}&lti=1"


def launch_target(db: Session, tenant_id: uuid.UUID, user: User, rid: str) -> str:
    """Check an LTI launch of ``cmi5:<rid>`` for ``user`` (raises lms.Cmi5Error), create or
    reuse the enrolment's registration, and return the canonical resource id to record."""
    lms.au_credential()  # fail closed, as a TrueNorth launch does
    release_id, au_index = _parse(rid)
    release = _release(db, tenant_id, release_id)
    _au_title(db, release, au_index)
    if user.tenant_id != tenant_id:
        raise lms.Cmi5Error(404, "course module not found")
    enrolled = lms.enrolment_for(db, user.id, release)  # 403 not enrolled, 409 another release
    if enrolled.status == EnrollmentStatus.withdrawn:
        raise lms.Cmi5Error(403, "your enrolment in this course has ended")
    lms.registration(db, enrolled, release)
    return resource_id(release.id, au_index)


def content_item(db: Session, tenant_id: uuid.UUID, rid: str) -> tuple[str, str] | None:
    """(resource id, title) of a picked AU, checked against the tenant; None if it is not one.
    Only the course's current (accepted) release: a superseded one is for the Students
    already pinned to it, not for new activities."""
    try:
        release_id, au_index = _parse(rid)
        release = _release(db, tenant_id, release_id)
        if release.state != ACCEPTED:
            return None
        title = _au_title(db, release, au_index)
    except lms.Cmi5Error:
        return None
    return resource_id(release.id, au_index), f"{release.title}: {title}"[:255]


def picker_items(db: Session, tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    """(``cmi5:<release>:<n>``, label) for every AU of the tenant's published courses'
    accepted releases, newest first. A release without a usable cmi5 package is skipped."""
    releases = (
        db.query(CourseRelease)
        .join(Course, Course.id == CourseRelease.course_id)
        .filter(
            CourseRelease.tenant_id == tenant_id,
            CourseRelease.state == ACCEPTED,
            Course.tenant_id == tenant_id,
            Course.is_published.is_(True),
        )
        .order_by(CourseRelease.created_at.desc())
        .limit(PICKER_SCAN)
        .all()
    )
    items: list[tuple[str, str]] = []
    listed = 0
    for release in releases:
        titles = _au_titles(db, release)
        if titles is None:  # no cmi5 package: does not count against the cap
            continue
        listed += 1
        for index, title in enumerate(titles):
            items.append((f"{RESOURCE_KIND}:{resource_id(release.id, index)}", f"{release.title}: {title}"))
        if listed >= PICKER_RELEASES:
            break
    return items
