"""Releases and publications, held by PostgreSQL itself (R2: CR1-14, CR1-15, F10, F12 in
docs/review/codereview1.md). Each test runs on a fresh database built by
``alembic upgrade head`` (tests/conftest.py ``postgres_engine``); skipped without
TEST_POSTGRES_ADMIN_URL.

* CR1-14: release immutability was only ORM hooks; a Core or raw SQL write went through.
* CR1-15: a publication's steps renewed its lease unconditionally, so a step that
  outlived the lease let a second process take the job while the first kept writing.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.course_publishing import service as publishing
from app.course_publishing.models import CoursePublication
from app.course_releases.models import ACCEPTED, CANDIDATE, CourseRelease, CourseReleaseBlob, EnrollmentReleasePin
from app.models import Course, Enrollment, ExternalPlatform, IntegrationAuthType, Tenant, User, UserRole
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


def _world(engine) -> dict:
    """A course with an accepted v1 and a candidate v2, one blob, one pinned enrollment."""
    with Session(engine) as s:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.flush()
        course = Course(name="C304", tenant_id=tenant.id)
        blob = CourseReleaseBlob(sha256=hashlib.sha256(b"x").hexdigest(), size=1, data=b"x")
        student = User(
            email=f"s{uuid.uuid4().hex[:6]}@x.test",
            display_name="s",
            role=UserRole.student,
            tenant_id=tenant.id,
            keycloak_id=f"kc-{uuid.uuid4().hex[:6]}",
        )
        s.add_all([course, blob, student])
        s.flush()

        def release(version: int, state: str) -> CourseRelease:
            digest = hashlib.sha256(f"{course.id}-{version}".encode()).hexdigest()
            return CourseRelease(
                tenant_id=tenant.id,
                course_id=course.id,
                catalogue_code="C304",
                arc2_code="C304",
                run_id=f"r{version}",
                slug="c304",
                title="C304",
                version=version,
                release_digest=digest,
                learner_digest=digest,
                platform_digest=digest,
                instructor_digest=digest,
                blob_sha256=blob.sha256,
                meta="{}",
                state=state,
            )

        v1, v2 = release(1, ACCEPTED), release(2, CANDIDATE)
        enrollment = Enrollment(user_id=student.id, course_id=course.id, tenant_id=tenant.id)
        s.add_all([v1, v2, enrollment])
        s.flush()
        s.add(EnrollmentReleasePin(enrollment_id=enrollment.id, release_id=v1.id))
        s.commit()
        return {
            "tenant": tenant.id,
            "course": course.id,
            "v1": v1.id,
            "v2": v2.id,
            "blob": blob.sha256,
            "enrollment": enrollment.id,
        }


def _refused(engine, sql: str, **params) -> None:
    with pytest.raises(DBAPIError, match="never|fixed|transition|written once"), engine.begin() as conn:
        conn.execute(text(sql), params)


# ── CR1-14: immutability in the database ──────────────────────────────


@pytest.mark.parametrize(
    "column", ["release_digest", "learner_digest", "instructor_digest", "blob_sha256", "meta", "version"]
)
def test_what_a_release_is_cannot_be_rewritten_by_raw_sql(postgres_engine, column):
    w = _world(postgres_engine)
    value = "1" if column == "meta" else ("99" if column == "version" else "f" * 64)
    _refused(postgres_engine, f"UPDATE course_releases SET {column} = :v WHERE id = :id", v=value, id=w["v1"])


def test_a_release_is_never_deleted_by_raw_sql(postgres_engine):
    w = _world(postgres_engine)
    _refused(postgres_engine, "DELETE FROM course_releases WHERE id = :id", id=w["v1"])


@pytest.mark.parametrize(("release", "to"), [("v1", "candidate"), ("v2", "superseded")])
def test_only_release_transitions_are_allowed(postgres_engine, release, to):
    w = _world(postgres_engine)
    _refused(postgres_engine, "UPDATE course_releases SET state = :s WHERE id = :id", s=to, id=w[release])


def test_the_acceptance_record_is_written_only_by_accepting(postgres_engine):
    w = _world(postgres_engine)
    _refused(postgres_engine, "UPDATE course_releases SET notes = 'rewritten' WHERE id = :id", id=w["v1"])
    _refused(postgres_engine, "UPDATE course_releases SET notes = 'early' WHERE id = :id", id=w["v2"])


def test_the_real_lifecycle_still_works(postgres_engine):
    """Supersede v1, accept v2 with its record: what app/course_releases/service.accept does."""
    w = _world(postgres_engine)
    with postgres_engine.begin() as conn:
        conn.execute(text("UPDATE course_releases SET state = 'superseded' WHERE id = :id"), {"id": w["v1"]})
        conn.execute(
            text(
                "UPDATE course_releases SET state = 'accepted', accepted_at = now(), notes = 'ok', "
                "acknowledged_actions = '[]' WHERE id = :id"
            ),
            {"id": w["v2"]},
        )
    with Session(postgres_engine) as s:
        assert s.get(CourseRelease, w["v2"]).state == ACCEPTED


def test_a_release_blob_is_never_rewritten_or_deleted(postgres_engine):
    w = _world(postgres_engine)
    _refused(postgres_engine, "UPDATE course_release_blobs SET data = 'y' WHERE sha256 = :h", h=w["blob"])
    _refused(postgres_engine, "DELETE FROM course_release_blobs WHERE sha256 = :h", h=w["blob"])


def test_an_enrollments_pin_never_moves(postgres_engine):
    """F10: a student's in-progress attempt keeps the release it started on."""
    w = _world(postgres_engine)
    _refused(
        postgres_engine,
        "UPDATE enrollment_release_pins SET release_id = :r WHERE enrollment_id = :e",
        r=w["v2"],
        e=w["enrollment"],
    )


# ── CR1-15: the publication lease has a holder ────────────────────────


def _publication(engine, w) -> uuid.UUID:
    with Session(engine) as s:
        platform = ExternalPlatform(
            name="m",
            slug=f"m-{uuid.uuid4().hex[:6]}",
            platform_type="fake",
            base_url="http://moodle.invalid",
            lti_issuer="http://moodle.invalid",
            auth_type=IntegrationAuthType.lti13,
            tenant_id=w["tenant"],
        )
        s.add(platform)
        s.flush()
        pub = CoursePublication(
            tenant_id=w["tenant"],
            release_id=w["v1"],
            course_id=w["course"],
            platform_id=platform.id,
            state="requested",
            stage_idnumber="tn-stage:x",
            live_idnumber=str(w["course"]),
        )
        s.add(pub)
        s.commit()
        return pub.id


def test_a_step_that_outlived_its_lease_writes_nothing_once_another_process_took_it(postgres_engine):
    w = _world(postgres_engine)
    pid = _publication(postgres_engine, w)
    with Session(postgres_engine) as first, Session(postgres_engine) as second:
        a = first.get(CoursePublication, pid)
        assert publishing.claim(first, a)
        first.commit()
        publishing._step(first, a, "staging")
        # The first run's next Moodle call outlives the lease; startup resume takes the job.
        with postgres_engine.begin() as conn:
            conn.execute(
                text("UPDATE course_publications SET lease_until = :t WHERE id = :id"),
                {"t": datetime.now(UTC) - timedelta(seconds=1), "id": pid},
            )
        b = second.get(CoursePublication, pid)
        assert publishing.claim(second, b)
        second.commit()
        publishing._step(second, b, "verifying")
        with pytest.raises(publishing.LeaseLostError):
            publishing._step(first, a, "activating")  # the first run wakes up
        with pytest.raises(publishing.LeaseLostError):
            publishing._fail(first, a, "the first run's Moodle call failed")
    with Session(postgres_engine) as s:
        pub = s.get(CoursePublication, pid)
        assert pub.state == "verifying" and pub.error == "", "the second run's state, untouched by the first"


def test_a_live_lease_cannot_be_claimed_twice(postgres_engine):
    w = _world(postgres_engine)
    pid = _publication(postgres_engine, w)
    with Session(postgres_engine) as first, Session(postgres_engine) as second:
        assert publishing.claim(first, first.get(CoursePublication, pid))
        first.commit()
        assert not publishing.claim(second, second.get(CoursePublication, pid))
