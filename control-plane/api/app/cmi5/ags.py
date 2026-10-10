"""AU results back to the launching LMS's gradebook (LTI 1.3 Assignment and Grade Services).

A Moodle activity deep-linked to a TrueNorth AU (resource ``cmi5:<release>:<n>``,
app/cmi5/lti.py) launches with an AGS line item in its claims; ``lti13.record_launch``
keeps it on the launch row. When TrueNorth, as the AU's cmi5 LMS, accepts the AU's
``passed``, ``failed`` or ``completed`` (app/cmi5/lms.py), :func:`enqueue` writes the AU's
result for every gradebook cell of that Student and AU, in the same transaction:

* ``scoreGiven`` is TrueNorth's own mark (``cmi5_grades``, out of 100). The rules already
  refuse a passed/failed whose score is not that mark (TN-GRADE); the score sent is the
  mark, never the number in the statement. ``completed`` alone sends no score
  (``gradingProgress`` Pending), and an AU's cmi5-allowed statements (no cmi5 category)
  never reach here: they are not results.
* A cell is (platform, line item, the platform's user id). One row each, holding the latest
  result, so a retried request or a second statement never posts something else. The
  result is resent only when it changes.
* Only a launch whose platform is active, in the release's tenant, granted the AGS score
  scope, and whose line item is on the platform's own origin. Nothing else gets a token.

Delivery is after commit (:func:`deliver_registration`, a background task of the AU's
statement request) and again from :func:`loop` (``CMI5_AGS_RETRY_SECONDS``, default 60):
a transient failure (network, 408/425/429, 5xx) is retried with backoff up to
``CMI5_AGS_MAX_ATTEMPTS`` (default 10); anything else is ``failed`` with the reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .. import lti13
from ..course_releases.models import CourseRelease
from ..models import ExternalPlatform, LTILaunch
from .models import AGS_FAILED, AGS_PENDING, AGS_SENT, Cmi5AgsScore, Cmi5Registration

logger = logging.getLogger("truenorth.cmi5.ags")

RESOURCE_KIND = "cmi5"
SCORE_MAXIMUM = 100
LEASE = timedelta(seconds=120)


def resource_id(release_id: uuid.UUID | str, au_index: int) -> str:
    """The LTI resource id of an AU: ``<release id>:<AU index>``."""
    return f"{release_id}:{au_index}"


def _now() -> datetime:
    return datetime.now(UTC)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url or "")
    return parts.scheme.lower(), parts.netloc.lower()


def _own_lineitem(platform: ExternalPlatform, lineitem: str) -> bool:
    """The line item is on the platform's registered origin (issuer, or its internal
    address): its access token goes nowhere else."""
    origin = _origin(lineitem)
    return bool(origin[1]) and origin in {_origin(platform.lti_issuer or ""), _origin(platform.base_url or "")}


def _cell_key(platform_id: uuid.UUID, lineitem: str, sub: str) -> str:
    return hashlib.sha256(f"{platform_id}\n{lineitem}\n{sub}".encode()).hexdigest()


def result_of(au_state: dict) -> tuple[float | None, str, str]:
    """(scoreGiven of 100, activityProgress, gradingProgress) for an AU's recorded state."""
    score = au_state.get("score")
    if isinstance(score, int | float) and not isinstance(score, bool):
        return round(float(score) * SCORE_MAXIMUM, 2), "Completed", "FullyGraded"
    return None, "Completed", "Pending"


def enqueue(db: Session, reg: Cmi5Registration, release: CourseRelease, au_index: int, au_state: dict) -> int:
    """Record the AU's result for each of the Student's gradebook cells. Returns how many
    rows now wait to be sent. Called in the transaction that recorded the result."""
    rid = resource_id(release.id, au_index)
    launches = (
        db.query(LTILaunch)
        .filter(
            LTILaunch.user_id == reg.user_id,
            LTILaunch.resource_kind == RESOURCE_KIND,
            LTILaunch.resource_id == rid,
            LTILaunch.ags_lineitem_url != "",
        )
        .order_by(LTILaunch.created_at.desc())
        .limit(50)
        .all()
    )
    score, activity, grading = result_of(au_state)
    now = _now()
    due = 0
    seen: set[str] = set()
    for launch in launches:
        key = _cell_key(launch.platform_id, launch.ags_lineitem_url, launch.lti_user_sub)
        if key in seen:
            continue
        seen.add(key)
        platform = db.get(ExternalPlatform, launch.platform_id)  # tenant-safe: compared with the release below
        if platform is None or not platform.is_active or platform.tenant_id != release.tenant_id:
            continue
        try:
            scopes = json.loads(launch.ags_scopes or "[]")
        except ValueError:
            scopes = []
        if lti13.AGS_SCORE_SCOPE not in (scopes if isinstance(scopes, list) else []):
            continue
        if not _own_lineitem(platform, launch.ags_lineitem_url):
            logger.warning("cmi5 AGS: launch %s names a line item off its platform's origin; not used", launch.id)
            continue
        # tenant-safe: the cell key is derived from this tenant's platform id.
        row = db.query(Cmi5AgsScore).filter(Cmi5AgsScore.cell_key == key).with_for_update().one_or_none()
        if row is None:
            row = Cmi5AgsScore(
                cell_key=key,
                tenant_id=release.tenant_id,
                platform_id=platform.id,
                registration_id=reg.id,
                user_id=reg.user_id,
                release_id=release.id,
                au_index=au_index,
                lineitem_url=launch.ags_lineitem_url,
                lti_user_sub=launch.lti_user_sub,
                attempts=0,
                last_error="",
            )
            db.add(row)
        elif (row.score_given, row.activity_progress, row.grading_progress) == (score, activity, grading) and (
            row.state in (AGS_SENT, AGS_PENDING)
        ):
            continue  # already sent, or on its way: nothing new for this cell
        row.score_given, row.activity_progress, row.grading_progress = score, activity, grading
        row.registration_id = reg.id
        row.result_at = now
        row.state = AGS_PENDING
        row.attempts = 0
        row.next_attempt_at = now
        row.last_error = ""
        due += 1
    db.flush()
    return due


def _payload(row: Cmi5AgsScore) -> dict:
    stamp = row.result_at if row.result_at.tzinfo else row.result_at.replace(tzinfo=UTC)
    out = {
        "timestamp": stamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "activityProgress": row.activity_progress,
        "gradingProgress": row.grading_progress,
    }
    if row.score_given is not None:
        out["scoreGiven"] = row.score_given
        out["scoreMaximum"] = SCORE_MAXIMUM
    return out


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 3600))


async def deliver(db: Session, row_id: uuid.UUID) -> str | None:
    """Send one due row, once, even with several senders. Returns its state afterwards, or
    None when it was not due (sent, failed, or another sender holds it)."""
    now = _now()
    claimed = db.execute(
        update(Cmi5AgsScore)
        .where(
            Cmi5AgsScore.id == row_id,
            Cmi5AgsScore.state == AGS_PENDING,
            or_(Cmi5AgsScore.next_attempt_at.is_(None), Cmi5AgsScore.next_attempt_at <= now),
        )
        .values(next_attempt_at=now + LEASE)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if claimed != 1:
        return None
    row = db.get(Cmi5AgsScore, row_id)  # tenant-safe: an outbox row, sent to its own platform only
    db.refresh(row)
    sent_result = row.result_at
    platform = db.get(ExternalPlatform, row.platform_id)  # tenant-safe: the row's own platform
    error: lti13.AGSError | None = None
    if platform is None or not platform.is_active or platform.tenant_id != row.tenant_id:
        error = lti13.AGSError("the platform is no longer registered and active", transient=False)
    else:
        try:
            await lti13.send_score(db, platform, row.lineitem_url, row.lti_user_sub, _payload(row))
        except lti13.AGSError as exc:
            error = exc
    db.refresh(row, with_for_update=True)
    if row.result_at != sent_result:  # a newer result arrived meanwhile: it goes next
        db.commit()
        return row.state
    if error is None:
        row.state, row.sent_at, row.last_error, row.next_attempt_at = AGS_SENT, _now(), "", None
        logger.info("cmi5 AGS: sent %s for AU %s of release %s", row.id, row.au_index, row.release_id)
    else:
        row.attempts += 1
        row.last_error = str(error)[:500]
        if error.transient and row.attempts < _int_env("CMI5_AGS_MAX_ATTEMPTS", 10):
            row.next_attempt_at = _now() + _backoff(row.attempts)
            logger.warning("cmi5 AGS: %s not sent (attempt %d, retried): %s", row.id, row.attempts, error)
        else:
            row.state, row.next_attempt_at = AGS_FAILED, None
            logger.error("cmi5 AGS: %s failed after %d attempt(s): %s", row.id, row.attempts, error)
    db.commit()
    return row.state


async def deliver_due(db: Session, *, registration_id: uuid.UUID | None = None, limit: int = 50) -> int:
    """Send the rows that are due (of one registration, or all). Returns how many were sent."""
    now = _now()
    q = db.query(Cmi5AgsScore.id).filter(
        Cmi5AgsScore.state == AGS_PENDING,
        or_(Cmi5AgsScore.next_attempt_at.is_(None), Cmi5AgsScore.next_attempt_at <= now),
    )
    if registration_id is not None:
        q = q.filter(Cmi5AgsScore.registration_id == registration_id)
    # tenant-safe: the background sender; each row goes to its own platform only.
    ids = [r for (r,) in q.order_by(Cmi5AgsScore.next_attempt_at).limit(limit).all()]
    sent = 0
    for row_id in ids:
        if await deliver(db, row_id) == AGS_SENT:
            sent += 1
    return sent


def open_session() -> Session:
    """The session background delivery uses (a seam for the tests)."""
    from ..db import SessionLocal

    return SessionLocal()


async def deliver_registration(registration_id: uuid.UUID) -> None:
    """Background task of the AU's statement request: send what it made due."""
    db = open_session()
    try:
        await deliver_due(db, registration_id=registration_id)
    except Exception:  # noqa: BLE001 — the retry loop picks up anything left
        logger.exception("cmi5 AGS: delivery for registration %s failed", registration_id)
    finally:
        db.close()


async def loop() -> None:
    """Resend what is due every ``CMI5_AGS_RETRY_SECONDS`` (0 turns it off)."""
    every = _int_env("CMI5_AGS_RETRY_SECONDS", 60)
    if every <= 0:
        return
    while True:
        await asyncio.sleep(every)
        db = open_session()
        try:
            await deliver_due(db)
        except Exception:  # noqa: BLE001 — the loop outlives any one round
            logger.exception("cmi5 AGS round failed")
        finally:
            db.close()
