"""Build a result-transfer package from one completed pseudonymous quiz attempt."""

from __future__ import annotations

import uuid
from datetime import UTC

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..course_releases.service import pinned_release
from ..models import CourseModule, Enrollment, Quiz, QuizAttempt
from ..settings import UnsafeSettingsError, enclave_id
from .contract import ResultClaim, ResultTransferError
from .pseudonymous import subject_token_for_user
from .service import QueuedResult, queue_result

QUIZ_SCORER_VERSION = "truenorth-quiz-v1"


def queue_quiz_attempt(
    db: Session,
    *,
    attempt_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    destination_enclave: str,
    signing_private_key: str,
    recipient_public_key: str,
) -> QueuedResult:
    """Seal a completed attempt using its learner-pinned immutable course release."""
    attempt = (
        db.query(QuizAttempt)
        .filter_by(id=attempt_id, tenant_id=tenant_id, user_id=user_id)
        .one_or_none()
    )
    if attempt is None:
        raise ResultTransferError("quiz attempt was not found for this learner and tenant")
    if attempt.submitted_at is None or attempt.passed is None:
        raise ResultTransferError("quiz attempt is not complete")
    if attempt.score < 0 or attempt.max_score <= 0 or attempt.score > attempt.max_score:
        raise ResultTransferError("quiz attempt has an invalid score")
    quiz = (
        db.query(Quiz)
        .filter(Quiz.id == attempt.quiz_id, or_(Quiz.tenant_id.is_(None), Quiz.tenant_id == tenant_id))
        .one_or_none()
    )
    if quiz is None or quiz.module_id is None:
        raise ResultTransferError("quiz attempt is not attached to a released course module")
    module = db.query(CourseModule).filter(CourseModule.id == quiz.module_id).one_or_none()
    if module is None:
        raise ResultTransferError("quiz course module was not found")
    enrollment = (
        db.query(Enrollment)
        .filter_by(user_id=user_id, course_id=module.course_id, tenant_id=tenant_id)
        .one_or_none()
    )
    if enrollment is None:
        raise ResultTransferError("learner is not enrolled in the quiz course for this tenant")
    release = pinned_release(db, enrollment.id)
    if release is None:
        raise ResultTransferError("learner enrollment has no pinned accepted course release")
    if release.tenant_id not in (None, tenant_id):
        raise ResultTransferError("pinned course release does not belong to this tenant")

    token = subject_token_for_user(db, tenant_id=tenant_id, user_id=user_id)
    occurred_at = attempt.submitted_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    claim = ResultClaim(
        opaque_subject_id=token,
        tenant_id=tenant_id,
        course_release_id=release.id,
        assessment_id=f"quiz:{quiz.id}",
        attempt_id=attempt.id,
        raw_score=attempt.score,
        max_score=attempt.max_score,
        outcome="pass" if attempt.passed else "fail",
        scorer_version=QUIZ_SCORER_VERSION,
        occurred_at=occurred_at,
    )
    try:
        source_enclave = enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc
    return queue_result(
        db,
        claim,
        source_enclave=source_enclave,
        destination_enclave=destination_enclave,
        signing_private_key=signing_private_key,
        recipient_public_key=recipient_public_key,
    )
