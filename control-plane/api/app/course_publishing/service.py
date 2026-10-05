"""Publish an accepted release to a Moodle: stage hidden, verify, then activate once.

    requested   the job exists (one per release and platform)
    staging     the whole course built as a hidden `tn-stage:<release>` course
    verifying   Moodle describes the staging course; every activity, quiz size and the
                LTI tool must be there, or nothing goes live
    activating  the live course (idnumber = TrueNorth course id) converged on the same
                payload and made visible, described again, the staging course deleted
    published   receipt recorded; earlier publications of this course there superseded

Until activation the live course is untouched, so students keep the previous release.
Every step is keyed by idnumbers, so running a step twice converges rather than
duplicating; a retry or a resumed job starts again from staging. Converging the live
course hides (never deletes) an activity a student has used.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .. import lti13
from ..course_releases.models import ACCEPTED, CourseRelease
from ..course_releases.service import load_bundle
from ..models import ExternalPlatform, Qualification
from ..moodle_backends import BaseMoodleBackend, MoodleError, get_moodle_backend
from . import payload as payload_mod
from .models import (
    ACTIVATING,
    FAILED,
    PUBLISHED,
    REQUESTED,
    RUNNING,
    STAGING,
    SUPERSEDED,
    VERIFYING,
    CoursePublication,
)

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=15)


class PublishRefusedError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def backend_for(db: Session, platform: ExternalPlatform) -> BaseMoodleBackend:
    def key() -> tuple[str, str]:
        k = lti13.get_tool_key(db)
        return k.private_key_pem, k.kid

    return get_moodle_backend(platform.platform_type, key_provider=key)


def request(
    db: Session, release: CourseRelease, platform: ExternalPlatform, *, user_id: uuid.UUID | None
) -> tuple[CoursePublication, bool]:
    """The publication of this release on this platform, created if new."""
    if release.state != ACCEPTED:
        raise PublishRefusedError(f"release v{release.version} is {release.state}; only the accepted release publishes")
    if platform.tenant_id != release.tenant_id:
        raise PublishRefusedError("platform not found", 404)
    try:
        get_moodle_backend(platform.platform_type)
    except ValueError as exc:
        raise PublishRefusedError(str(exc), 422) from exc
    pub = (
        db.query(CoursePublication)
        .filter(CoursePublication.release_id == release.id, CoursePublication.platform_id == platform.id)
        .one_or_none()
    )
    if pub is not None:
        return pub, False
    pub = CoursePublication(
        tenant_id=release.tenant_id,
        release_id=release.id,
        course_id=release.course_id,
        platform_id=platform.id,
        state=REQUESTED,
        stage_idnumber=payload_mod.stage_idnumber(release.id),
        live_idnumber=str(release.course_id),
        requested_by=user_id,
    )
    db.add(pub)
    db.flush()
    return pub, True


def claim(db: Session, pub: CoursePublication) -> bool:
    """Take the job's lease; False if another process holds a live one."""
    now = datetime.now(UTC)
    result = db.execute(
        update(CoursePublication)
        .where(
            CoursePublication.id == pub.id,
            or_(CoursePublication.lease_until.is_(None), CoursePublication.lease_until < now),
        )
        .values(lease_until=now + LEASE)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    db.refresh(pub)
    return result.rowcount == 1


def _category(db: Session, release: CourseRelease) -> dict[str, str]:
    from ..models import Course

    course = db.get(Course, release.course_id)
    qual = db.get(Qualification, course.qualification_id) if course and course.qualification_id else None
    if qual is not None:
        return {"idnumber": f"tn-qual:{qual.id}", "name": qual.title or qual.nqual or "Qualification"}
    return {"idnumber": "tn-catalogue", "name": "TrueNorth courses"}


def _remote(pub: CoursePublication, step: str, data: Any) -> None:
    remote = json.loads(pub.remote or "{}")
    remote[step] = data
    pub.remote = json.dumps(remote, sort_keys=True, default=str)


def _verify(described: dict[str, Any], want: dict[str, dict[str, Any]], sections: int, *, visible: bool) -> list[str]:
    problems = []
    if not described.get("exists"):
        return ["the course does not exist in Moodle"]
    if bool(described.get("visible")) != visible:
        problems.append(f"course visibility is {described.get('visible')}, expected {int(visible)}")
    if described.get("sections", 0) < sections:
        problems.append(f"{described.get('sections')} sections, expected {sections}")
    acts = described.get("activities") or {}
    for idn, spec in want.items():
        got = acts.get(idn)
        if got is None:
            problems.append(f"{idn} is missing")
            continue
        if got.get("type") != spec["type"] or not got.get("visible"):
            problems.append(f"{idn} is {got.get('type')} visible={got.get('visible')}, expected visible {spec['type']}")
        if "questions" in spec and got.get("questions") != spec["questions"]:
            problems.append(f"{idn} has {got.get('questions')} questions, expected {spec['questions']}")
    if any(s["type"] == "lti" for s in want.values()) and not described.get("ltitool"):
        problems.append("the TrueNorth Range LTI tool is not registered on this Moodle")
    return problems


def run(db: Session, pub: CoursePublication, *, backend: BaseMoodleBackend | None = None) -> CoursePublication:
    """Drive a publication to published or failed. Commits after every step, so a crash
    leaves the job resumable at the step it reached."""
    if pub.state == PUBLISHED:
        return pub
    if not claim(db, pub):
        raise PublishRefusedError("this publication is already running")
    release = db.get(CourseRelease, pub.release_id)
    platform = db.get(ExternalPlatform, pub.platform_id)
    backend = backend or backend_for(db, platform)
    pub.attempts += 1
    pub.error = ""
    try:
        bundle = load_bundle(db, release)
        category = _category(db, release)
        common = {"release_id": release.id, "category": category, "version": release.version}
        stage = payload_mod.build(bundle, idnumber=pub.stage_idnumber, visible=False, **common)
        live = payload_mod.build(bundle, idnumber=pub.live_idnumber, visible=True, **common)
        want = payload_mod.expected(live)
        pub.payload_digest = payload_mod.digest(live)

        _step(db, pub, STAGING)
        _remote(pub, "staging", backend.upsert_course(platform, stage))
        _step(db, pub, VERIFYING)
        described = backend.describe_course(platform, pub.stage_idnumber)
        _remote(pub, "verify", described)
        problems = _verify(described, payload_mod.expected(stage), len(stage["sections"]), visible=False)
        if problems:
            return _fail(db, pub, "staging course did not verify: " + "; ".join(problems))

        _step(db, pub, ACTIVATING)
        _remote(pub, "activate", backend.upsert_course(platform, live))
        described = backend.describe_course(platform, pub.live_idnumber)
        problems = _verify(described, want, len(live["sections"]), visible=True)
        if problems:
            return _fail(db, pub, "live course did not verify after activation: " + "; ".join(problems))
        _remote(pub, "cleanup", backend.delete_stage(platform, pub.stage_idnumber))
    except MoodleError as exc:
        return _fail(db, pub, str(exc))

    now = datetime.now(UTC)
    pub.receipt = json.dumps(
        {
            "moodle_course_id": described.get("courseid"),
            "activities": {k: v.get("cmid") for k, v in (described.get("activities") or {}).items() if k in want},
            "release_id": str(release.id),
            "release_version": release.version,
            "release_digest": release.release_digest,
            "payload_digest": pub.payload_digest,
            "published_at": now.isoformat(),
        },
        sort_keys=True,
    )
    for older in (
        db.query(CoursePublication)
        .filter(
            CoursePublication.course_id == pub.course_id,
            CoursePublication.platform_id == pub.platform_id,
            CoursePublication.state == PUBLISHED,
            CoursePublication.id != pub.id,
        )
        .all()
    ):
        older.state = SUPERSEDED
    pub.state = PUBLISHED
    pub.published_at = now
    pub.lease_until = None
    db.commit()
    logger.info("published release %s to platform %s (course %s)", release.id, platform.id, described.get("courseid"))
    return pub


def _step(db: Session, pub: CoursePublication, state: str) -> None:
    pub.state = state
    db.commit()


def _fail(db: Session, pub: CoursePublication, error: str) -> CoursePublication:
    pub.state = FAILED
    pub.error = error[:4000]
    pub.lease_until = None
    db.commit()
    logger.warning("publication %s failed: %s", pub.id, error)
    return pub


def retry(db: Session, pub: CoursePublication) -> CoursePublication:
    if pub.state == PUBLISHED:
        raise PublishRefusedError("already published")
    if pub.state == SUPERSEDED:
        raise PublishRefusedError("a later release has been published here")
    pub.state = REQUESTED
    db.flush()
    return pub


def stalled(db: Session) -> list[CoursePublication]:
    """Jobs a dead process left mid-way: running state, lease lapsed."""
    now = datetime.now(UTC)
    return (
        db.query(CoursePublication)
        .filter(
            CoursePublication.state.in_(RUNNING),
            or_(CoursePublication.lease_until.is_(None), CoursePublication.lease_until < now),
        )
        .all()
    )


def resume_stalled(db: Session) -> int:
    """Run every stalled job again from staging. Returns how many were resumed."""
    count = 0
    for pub in stalled(db):
        try:
            run(db, pub)
            count += 1
        except PublishRefusedError:
            continue
    return count
