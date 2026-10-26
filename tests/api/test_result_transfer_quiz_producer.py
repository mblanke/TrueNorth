from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from app.course_releases.models import ACCEPTED, CourseRelease, CourseReleaseBlob, EnrollmentReleasePin
from app.models import Course, CourseModule, Enrollment, ModuleContentType, Quiz, QuizAttempt
from app.result_transfer.contract import ResultTransferError, generate_recipient_keypair, generate_signing_keypair
from app.result_transfer.pseudonymous import assign_account
from app.result_transfer.quiz_producer import QUIZ_SCORER_VERSION, queue_quiz_attempt
from app.result_transfer.service import import_subject_credential, issue_subject_credential

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
SECRET_USER = TENANT


def _world(db, monkeypatch):
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    credential_private, credential_public = generate_signing_keypair()
    credential, _ = issue_subject_credential(
        db,
        tenant_id=TENANT,
        user_id=SECRET_USER,
        destination_enclave="unclass",
        signing_private_key=credential_private,
    )
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    available, _ = import_subject_credential(
        db,
        credential,
        trusted_signing_public_key=credential_public,
        source_enclave="secret",
    )
    _, learner = assign_account(
        db,
        credential_id=available.credential_id,
        tenant_id=TENANT,
        keycloak_subject=str(uuid.uuid4()),
    )

    course = Course(name="Pinned quiz course", tenant_id=TENANT)
    db.add(course)
    db.flush()
    module = CourseModule(
        course_id=course.id,
        ordinal=1,
        title="Assessment",
        content_type=ModuleContentType.quiz,
    )
    db.add(module)
    db.flush()
    quiz = Quiz(title="Final", module_id=module.id, tenant_id=TENANT, is_published=True)
    enrollment = Enrollment(user_id=learner.id, course_id=course.id, tenant_id=TENANT)
    db.add_all([quiz, enrollment])
    db.flush()
    digest = "d" * 64
    db.add(CourseReleaseBlob(sha256=digest, size=2, data=b"{}"))
    db.flush()
    release = CourseRelease(
        tenant_id=TENANT,
        course_id=course.id,
        catalogue_code="COTE-QZ",
        arc2_code="COTE-QZ",
        run_id="quiz-result",
        slug="quiz-result",
        title="Pinned quiz course",
        version=1,
        release_digest=digest,
        learner_digest=digest,
        platform_digest=digest,
        instructor_digest=digest,
        blob_sha256=digest,
        meta="{}",
        state=ACCEPTED,
    )
    db.add(release)
    db.flush()
    db.add(EnrollmentReleasePin(enrollment_id=enrollment.id, release_id=release.id))
    attempt = QuizAttempt(
        quiz_id=quiz.id,
        user_id=learner.id,
        tenant_id=TENANT,
        score=8,
        max_score=10,
        passed=True,
        submitted_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
    )
    db.add(attempt)
    db.flush()
    return available, learner, release, attempt


def test_completed_quiz_queues_pinned_release_and_opaque_subject(db_session, monkeypatch) -> None:
    credential, learner, release, attempt = _world(db_session, monkeypatch)
    signing_private, _ = generate_signing_keypair()
    _, recipient_public = generate_recipient_keypair()

    queued = queue_quiz_attempt(
        db_session,
        attempt_id=attempt.id,
        tenant_id=TENANT,
        user_id=learner.id,
        destination_enclave="secret",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )

    assert queued.projection.opaque_subject_id == credential.opaque_subject_id
    assert queued.projection.course_release_id == release.id
    assert queued.projection.assessment_id == f"quiz:{attempt.quiz_id}"
    assert queued.projection.attempt_id == attempt.id and queued.projection.outcome == "pass"
    assert "raw_score" not in queued.row.package_json and "max_score" not in queued.row.package_json
    assert json.loads(queued.row.package_json)["ciphertext"]
    assert QUIZ_SCORER_VERSION not in queued.row.package_json


def test_quiz_producer_refuses_unsubmitted_or_unpinned_attempt(db_session, monkeypatch) -> None:
    _, learner, _, attempt = _world(db_session, monkeypatch)
    signing_private, _ = generate_signing_keypair()
    _, recipient_public = generate_recipient_keypair()
    attempt.submitted_at = None

    with pytest.raises(ResultTransferError, match="not complete"):
        queue_quiz_attempt(
            db_session,
            attempt_id=attempt.id,
            tenant_id=TENANT,
            user_id=learner.id,
            destination_enclave="secret",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
        )
    attempt.submitted_at = datetime(2026, 10, 24, 12, tzinfo=UTC)
    pin = db_session.query(EnrollmentReleasePin).one()
    db_session.delete(pin)
    db_session.flush()
    with pytest.raises(ResultTransferError, match="no pinned"):
        queue_quiz_attempt(
            db_session,
            attempt_id=attempt.id,
            tenant_id=TENANT,
            user_id=learner.id,
            destination_enclave="secret",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
        )
