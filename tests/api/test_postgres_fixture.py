"""The PostgreSQL fixture (tests/conftest.py ``postgres_engine``) is the schema production
runs: built by ``alembic upgrade head``, so its constraints are the migrations', not
``create_all``'s. The concurrency tests for releases, publications and labs (CR1-13 in
docs/review/codereview1.md) build on it. Skipped without TEST_POSTGRES_ADMIN_URL."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.course_releases.models import ACCEPTED, CANDIDATE, CourseRelease, CourseReleaseBlob
from app.models import Course
from conftest import API_DIR
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def test_the_database_is_at_the_single_migration_head(postgres_engine):
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"the migration chain has {len(heads)} heads: {heads}"
    with postgres_engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalars().all() == heads


def _release(course: Course, blob: CourseReleaseBlob, version: int, state: str) -> CourseRelease:
    digest = hashlib.sha256(f"{course.id}-{version}".encode()).hexdigest()
    return CourseRelease(
        course_id=course.id,
        catalogue_code="C304",
        arc2_code="C304",
        run_id=f"run-{version}",
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


def test_one_accepted_release_per_course_is_enforced_by_the_migrated_schema(postgres_engine):
    with Session(postgres_engine) as db:
        course = Course(name=f"course-{uuid.uuid4().hex[:6]}")
        blob = CourseReleaseBlob(sha256=hashlib.sha256(b"x").hexdigest(), size=1, data=b"x")
        db.add_all([course, blob])
        db.flush()
        db.add_all([_release(course, blob, 1, ACCEPTED), _release(course, blob, 2, CANDIDATE)])
        db.commit()

        db.add(_release(course, blob, 3, ACCEPTED))
        with pytest.raises(IntegrityError, match="uq_course_release_accepted"):
            db.commit()
