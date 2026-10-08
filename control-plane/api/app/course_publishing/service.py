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

from sqlalchemy import and_, exists, or_, update
from sqlalchemy.orm import Session, aliased

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
HOLDERS = "course_publication_holders"  # db.info: publication id -> this run's lease token


class PublishRefusedError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class LeaseLostError(RuntimeError):
    """Another process took this publication's lease (this run's step outlived it). This
    run stops at once and writes nothing more: the job is the other process's now."""


def backend_for(db: Session, platform: ExternalPlatform) -> BaseMoodleBackend:
    def key() -> tuple[str, str]:
        k = lti13.get_tool_key(db)
        return lti13.signing_pem(k), k.kid

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
    """Take the job's lease; False if another process holds a live one, or if another
    publication of the same course is running on the same Moodle. All of a course's
    publications write one live Moodle course: two at once let an older release's upsert
    land after a newer one's, while the records said the newer one was live. The lease is
    this run's (``lease_holder``): every later write renews it only while it still is."""
    now = datetime.now(UTC)
    holder = uuid.uuid4().hex
    other = aliased(CoursePublication)
    sibling_running = exists().where(
        and_(
            other.course_id == pub.course_id,
            other.platform_id == pub.platform_id,
            other.id != pub.id,
            other.lease_until.isnot(None),
            other.lease_until >= now,
        )
    )
    result = db.execute(
        update(CoursePublication)
        .where(
            CoursePublication.id == pub.id,
            or_(CoursePublication.lease_until.is_(None), CoursePublication.lease_until < now),
            ~sibling_running,
        )
        .values(lease_until=now + LEASE, lease_holder=holder)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    db.refresh(pub)
    if result.rowcount == 1:
        db.info.setdefault(HOLDERS, {})[pub.id] = holder
        return True
    return False


def _commit_holding(db: Session, pub: CoursePublication, *, release_lease: bool = False) -> None:
    """Commit this step's writes only while this run still holds the lease, renewing it
    (or giving it up at the end) in the same transaction. A step that outlived the lease
    used to renew it unconditionally, so two processes drove one job (CR1-15)."""
    holder = db.info.get(HOLDERS, {}).get(pub.id)
    values = (
        {"lease_until": None, "lease_holder": None} if release_lease else {"lease_until": datetime.now(UTC) + LEASE}
    )
    kept = (
        holder is not None
        and db.execute(
            update(CoursePublication)
            .where(CoursePublication.id == pub.id, CoursePublication.lease_holder == holder)
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
        == 1
    )
    if not kept:
        db.rollback()
        raise LeaseLostError(f"publication {pub.id}: another process holds its lease now")
    pub.lease_until = values["lease_until"]
    if release_lease:
        pub.lease_holder = None
        db.info.get(HOLDERS, {}).pop(pub.id, None)
    db.commit()


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
        if got.get("section") is not None and got.get("section") != spec["section"]:
            problems.append(f"{idn} is in section {got.get('section')}, expected {spec['section']}")
        if "questions" in spec and got.get("questions") != spec["questions"]:
            problems.append(f"{idn} has {got.get('questions')} questions, expected {spec['questions']}")
        if spec.get("content") and not got.get("content_length", 1):
            problems.append(f"{idn} is an empty page")
        if "sha1" in spec and got.get("sha1") != spec["sha1"]:
            problems.append(f"{idn} holds a different file than the release")
    extra = sorted(k for k, a in acts.items() if k not in want and a.get("visible"))
    if extra:
        problems.append(f"visible TrueNorth activities this release does not have: {', '.join(extra)}")
    if any(s["type"] == "lti" for s in want.values()) and not described.get("ltitool"):
        problems.append("the TrueNorth Range LTI tool is not registered on this Moodle")
    return problems


def _same_course(db: Session, pub: CoursePublication):
    return db.query(CoursePublication).filter(
        CoursePublication.course_id == pub.course_id,
        CoursePublication.platform_id == pub.platform_id,
        CoursePublication.id != pub.id,
    )


def _newer_than(db: Session, pub: CoursePublication, release: CourseRelease) -> bool:
    """Whether a later release of this course is already published on this Moodle."""
    for other in _same_course(db, pub).filter(CoursePublication.state == PUBLISHED).all():
        other_release = db.get(CourseRelease, other.release_id)
        if other_release is not None and other_release.version > release.version:
            return True
    return False


def run(db: Session, pub: CoursePublication, *, backend: BaseMoodleBackend | None = None) -> CoursePublication:
    """Drive a publication to published or failed. Commits after every step, so a crash
    leaves the job resumable at the step it reached. Only the course's accepted release is
    ever published: a job for a release that has since been superseded ends superseded,
    whether it is retried by hand or resumed at startup, so Moodle never rolls back."""
    if pub.state in (PUBLISHED, SUPERSEDED):
        return pub
    release = db.get(CourseRelease, pub.release_id)
    if release is None or release.state != ACCEPTED or _newer_than(db, pub, release):
        pub.state = SUPERSEDED
        pub.error = "a later release of this course has been accepted"
        pub.lease_until = pub.lease_holder = None  # a run still going stops at its next step
        db.commit()
        return pub
    if not claim(db, pub):
        raise PublishRefusedError(
            "this publication is already running, or another release of this course is being published to"
            " this Moodle; it runs after that one"
        )
    platform = db.get(ExternalPlatform, pub.platform_id)
    pub.attempts += 1
    pub.error = ""
    warnings: list[str] = []
    try:
        backend = backend or backend_for(db, platform)
        bundle = load_bundle(db, release)
        category = _category(db, release)
        common = {
            "release_id": release.id,
            "course_id": release.course_id,
            "category": category,
            "version": release.version,
        }
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
    except LeaseLostError:
        raise  # the job is another process's now: write nothing
    except MoodleError as exc:
        return _fail(db, pub, str(exc))
    except Exception as exc:  # noqa: BLE001 — anything else is recorded, never left running
        logger.exception("publication %s crashed", pub.id)
        return _fail(db, pub, f"publication crashed: {type(exc).__name__}: {exc}")
    try:
        # Students are already on the new release; a stage left behind is housekeeping.
        _remote(pub, "cleanup", backend.delete_stage(platform, pub.stage_idnumber))
    except MoodleError as exc:
        warnings.append(f"the staging course {pub.stage_idnumber} was not deleted: {exc}")

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
            "warnings": warnings,
        },
        sort_keys=True,
    )
    for older in _same_course(db, pub).all():
        older_release = db.get(CourseRelease, older.release_id)
        if older.state != SUPERSEDED and older_release is not None and older_release.version < release.version:
            older.state = SUPERSEDED
            older.lease_until = older.lease_holder = None
    pub.state = PUBLISHED
    pub.published_at = now
    _commit_holding(db, pub, release_lease=True)
    logger.info("published release %s to platform %s (course %s)", release.id, platform.id, described.get("courseid"))
    return pub


def _step(db: Session, pub: CoursePublication, state: str) -> None:
    """Move to the next step and renew the lease, so a slow Moodle never lets a second
    runner take over a job that is still making progress."""
    pub.state = state
    _commit_holding(db, pub)


def _fail(db: Session, pub: CoursePublication, error: str) -> CoursePublication:
    pub.state = FAILED
    pub.error = error[:4000]
    _commit_holding(db, pub, release_lease=True)
    logger.warning("publication %s failed: %s", pub.id, error)
    return pub


def retry(db: Session, pub: CoursePublication) -> CoursePublication:
    if pub.state == PUBLISHED:
        raise PublishRefusedError("already published")
    release = db.get(CourseRelease, pub.release_id)
    if pub.state == SUPERSEDED or release is None or release.state != ACCEPTED:
        raise PublishRefusedError("a later release of this course has been accepted; publish that one")
    pub.state = REQUESTED
    db.flush()
    return pub


def stalled(db: Session) -> list[CoursePublication]:
    """Jobs a dead process left mid-way (running state, lease lapsed), and jobs still
    requested: a process that died before running one, or one that waited for another
    release of its course and was never started. Oldest first."""
    now = datetime.now(UTC)
    return (
        db.query(CoursePublication)
        .filter(
            CoursePublication.state.in_((*RUNNING, REQUESTED)),
            or_(CoursePublication.lease_until.is_(None), CoursePublication.lease_until < now),
        )
        .order_by(CoursePublication.created_at)
        .all()
    )


def waiting(db: Session, course_id: uuid.UUID, platform_id: uuid.UUID) -> CoursePublication | None:
    """The newest requested publication of this course on this Moodle (an older one would
    end superseded anyway), or None."""
    pubs = (
        db.query(CoursePublication)
        .filter(
            CoursePublication.course_id == course_id,
            CoursePublication.platform_id == platform_id,
            CoursePublication.state == REQUESTED,
        )
        .all()
    )

    def version(p: CoursePublication) -> int:
        release = db.get(CourseRelease, p.release_id)
        return release.version if release is not None else 0

    return max(pubs, key=version, default=None)


def resume_stalled(db: Session) -> int:
    """Run every stalled job again from staging. Returns how many were resumed."""
    count = 0
    for pub in stalled(db):
        try:
            run(db, pub)
            count += 1
        except (PublishRefusedError, LeaseLostError):
            continue
    return count
