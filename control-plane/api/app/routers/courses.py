"""TrueNorth Range - Courses, Enrollments & Learning Paths router.

Provides CRUD for courses (with ordered modules), user enrollments,
progress tracking, learning paths, and unified transcript generation.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    Exercise,
    ExternalActivity,
    LearningPath,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    User,
)
from ..schemas import (
    CourseIn,
    CourseListOut,
    CourseOut,
    CourseUpdate,
    EnrollmentIn,
    EnrollmentOut,
    LearningPathIn,
    LearningPathOut,
    ModuleProgressOut,
    PaginatedResponse,
    TranscriptEntry,
    TranscriptOut,
)

logger = logging.getLogger("truenorth.courses")

router = APIRouter(prefix="/courses", tags=["courses"])


# ══════════════════════════════════════════════════════════════════════════
# Course CRUD
# ══════════════════════════════════════════════════════════════════════════


@router.post("", response_model=CourseOut, status_code=status.HTTP_201_CREATED)
def create_course(
    body: CourseIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new course with optional ordered modules."""
    course = Course(
        name=body.name,
        description=body.description,
        version=body.version,
        difficulty=body.difficulty,
        duration_hours=body.duration_hours,
        tags=json.dumps(body.tags),
        nice_work_roles=json.dumps(body.nice_work_roles),
        is_published=body.is_published,
        tenant_id=user.tenant_id,
    )
    db.add(course)
    db.flush()

    for idx, mod in enumerate(body.modules):
        cm = CourseModule(
            course_id=course.id,
            ordinal=idx,
            title=mod.title,
            description=mod.description,
            content_type=ModuleContentType(mod.content_type),
            content_ref=mod.content_ref,
            duration_minutes=mod.duration_minutes,
            is_required=mod.is_required,
            pass_threshold=mod.pass_threshold,
        )
        db.add(cm)

    db.commit()
    db.refresh(course)
    logger.info("Course created: %s (%s)", course.name, course.id)
    return course


@router.get("", response_model=PaginatedResponse[CourseListOut])
def list_courses(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    published_only: bool = Query(default=False),
    difficulty: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List courses with optional filters."""
    q = db.query(Course)
    if published_only:
        q = q.filter(Course.is_published == True)
    if difficulty:
        q = q.filter(Course.difficulty == difficulty)
    total = q.count()
    items = q.order_by(Course.created_at.desc()).offset(offset).limit(limit).all()
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{course_id}", response_model=CourseOut)
def get_course(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single course with its modules."""
    course = (
        db.query(Course)
        .options(joinedload(Course.modules))
        .filter(Course.id == course_id)
        .first()
    )
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return course


@router.patch("/{course_id}", response_model=CourseOut)
def update_course(
    course_id: uuid.UUID,
    body: CourseUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Update course metadata."""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    updates = body.model_dump(exclude_unset=True)
    if "tags" in updates:
        updates["tags"] = json.dumps(updates["tags"])
    if "nice_work_roles" in updates:
        updates["nice_work_roles"] = json.dumps(updates["nice_work_roles"])
    for k, v in updates.items():
        setattr(course, k, v)
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a course and its modules."""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    db.query(CourseModule).filter(CourseModule.course_id == course_id).delete()
    db.delete(course)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════
# Enrollments
# ══════════════════════════════════════════════════════════════════════════


@router.post("/{course_id}/enroll", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED)
def enroll_user(
    course_id: uuid.UUID,
    body: EnrollmentIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Enroll a user in a course."""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    # Check for existing enrollment
    existing = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == body.user_id, Enrollment.course_id == course_id)
        .first()
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "User already enrolled in this course")
    enrollment = Enrollment(
        user_id=body.user_id,
        course_id=course_id,
        tenant_id=user.tenant_id,
        status=EnrollmentStatus.enrolled,
    )
    db.add(enrollment)
    db.flush()

    # Create ModuleProgress entries for each module
    modules = db.query(CourseModule).filter(CourseModule.course_id == course_id).order_by(CourseModule.ordinal).all()
    for mod in modules:
        mp = ModuleProgress(
            enrollment_id=enrollment.id,
            module_id=mod.id,
        )
        db.add(mp)

    db.commit()
    db.refresh(enrollment)
    logger.info("User %s enrolled in course %s", body.user_id, course_id)
    return enrollment


@router.get("/{course_id}/enrollments", response_model=list[EnrollmentOut])
def list_enrollments(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all enrollments for a course."""
    return (
        db.query(Enrollment)
        .filter(Enrollment.course_id == course_id)
        .order_by(Enrollment.enrolled_at.desc())
        .all()
    )


@router.get("/{course_id}/progress/{user_id}", response_model=list[ModuleProgressOut])
def get_user_progress(
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get per-module progress for a user in a course."""
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id)
        .first()
    )
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    return (
        db.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id)
        .all()
    )


@router.post("/{course_id}/complete/{user_id}", response_model=EnrollmentOut)
def complete_course(
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Mark a course enrollment as completed and calculate final grade."""
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id)
        .first()
    )
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")

    # Calculate final score from module progress
    progress_rows = (
        db.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id)
        .all()
    )
    total_score = sum(p.score for p in progress_rows)
    max_score = sum(p.max_score for p in progress_rows) or 1
    pct = (total_score / max_score) * 100

    # Letter grade
    if pct >= 90:
        grade = "A"
    elif pct >= 80:
        grade = "B"
    elif pct >= 70:
        grade = "C"
    elif pct >= 60:
        grade = "D"
    else:
        grade = "F"

    enrollment.status = EnrollmentStatus.completed
    enrollment.completed_at = datetime.now(timezone.utc)
    enrollment.final_score = total_score
    enrollment.max_score = max_score
    enrollment.final_grade = grade

    db.commit()
    db.refresh(enrollment)
    logger.info("Course %s completed for user %s — grade %s", course_id, user_id, grade)
    return enrollment


# ══════════════════════════════════════════════════════════════════════════
# Learning Paths
# ══════════════════════════════════════════════════════════════════════════

lp_router = APIRouter(prefix="/learning-paths", tags=["learning-paths"])


@lp_router.post("", response_model=LearningPathOut, status_code=status.HTTP_201_CREATED)
def create_learning_path(
    body: LearningPathIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a learning path (ordered sequence of courses)."""
    lp = LearningPath(
        name=body.name,
        description=body.description,
        course_ids=json.dumps([str(c) for c in body.course_ids]),
        is_published=body.is_published,
        tenant_id=user.tenant_id,
    )
    db.add(lp)
    db.commit()
    db.refresh(lp)
    return lp


@lp_router.get("", response_model=list[LearningPathOut])
def list_learning_paths(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all learning paths."""
    return db.query(LearningPath).order_by(LearningPath.created_at.desc()).all()


@lp_router.get("/{lp_id}", response_model=LearningPathOut)
def get_learning_path(
    lp_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single learning path."""
    lp = db.query(LearningPath).filter(LearningPath.id == lp_id).first()
    if not lp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Learning path not found")
    return lp


# ══════════════════════════════════════════════════════════════════════════
# User Transcript (unified cross-platform learning record)
# ══════════════════════════════════════════════════════════════════════════

transcript_router = APIRouter(prefix="/users", tags=["transcript"])


@transcript_router.get("/{user_id}/transcript", response_model=TranscriptOut)
def get_transcript(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Generate a unified learning transcript from all sources."""
    entries: list[TranscriptEntry] = []

    # 1. TrueNorth exercises
    exercises = db.query(Exercise).filter(
        Exercise.tenant_id == user.tenant_id
    ).all()
    for ex in exercises:
        entries.append(TranscriptEntry(
            source="truenorth",
            activity_type="exercise",
            title=ex.name,
            score=ex.total_score,
            max_score=ex.max_score,
            completed_at=ex.completed_at,
        ))

    # 2. Course enrollments
    enrollments = db.query(Enrollment).filter(Enrollment.user_id == user_id).all()
    for enr in enrollments:
        course = db.query(Course).filter(Course.id == enr.course_id).first()
        if course:
            entries.append(TranscriptEntry(
                source="truenorth",
                activity_type="course",
                title=course.name,
                score=enr.final_score,
                max_score=enr.max_score,
                grade=enr.final_grade,
                completed_at=enr.completed_at,
            ))

    # 3. External activities
    ext_activities = db.query(ExternalActivity).filter(ExternalActivity.user_id == user_id).all()
    for ea in ext_activities:
        entries.append(TranscriptEntry(
            source=ea.activity_type,
            activity_type=ea.activity_type,
            title=ea.title,
            score=ea.score,
            max_score=ea.max_score,
            completed_at=ea.completed_at,
        ))

    # 4. Certifications
    from ..models import Certification
    from ..schemas import CertificationOut
    certs = db.query(Certification).filter(Certification.user_id == user_id).all()
    cert_outs = [CertificationOut.model_validate(c) for c in certs]

    return TranscriptOut(
        user_id=user_id,
        entries=entries,
        total_entries=len(entries),
        certifications=cert_outs,
    )