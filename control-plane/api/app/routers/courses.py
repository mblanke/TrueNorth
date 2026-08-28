"""TrueNorth Range - Courses, Enrollments & Learning Paths router.

Provides CRUD for courses (with ordered modules), user enrollments,
progress tracking, learning paths, and unified transcript generation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload

from .. import course_content_ingest, programme_ingest, qsp_paths
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..enrollment import ensure_enrollment, ensure_path_enrollment
from ..models import (
    ContentKind,
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    Exercise,
    ExternalActivity,
    LearningPath,
    Lesson,
    ModuleContent,
    ModuleContentType,
    ModuleProgress,
    PerformanceObjective,
    Qualification,
    Quiz,
    SecurityGroup,
    SecurityGroupMembership,
    User,
)
from ..rbac import Permission, require_permission, user_has_permission
from ..schemas import (
    CourseIn,
    CourseListOut,
    CourseOut,
    CourseUpdate,
    EnrollmentIn,
    EnrollmentOut,
    LearningPathGroupAssignIn,
    LearningPathIn,
    LearningPathOut,
    LearningPathUpdate,
    ModuleProgressOut,
    PaginatedResponse,
    TranscriptEntry,
    TranscriptOut,
)
from ..tenancy import get_owned, tenant_uuid

logger = logging.getLogger("truenorth.courses")

# Upload ceiling for the programme catalogue CSV (mirrors qsp.MAX_CSV_BYTES).
MAX_CSV_BYTES = 4 * 1024 * 1024

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
    tags: list[str] | None = Query(default=None),
    include_retired: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List courses with optional filters.

    `tags` is repeatable and ANDs — `?tags=DP1&tags=delivers:none` is "DP1 courses that
    deliver no objective". Tags are derived at import from `course_meta`
    (`programme_ingest.catalogue_tags`), never hand-authored.

    Retired courses are hidden by default: they are spine-generated stubs that authored
    content has superseded, kept only so their scenario/range wiring survives.
    """
    q = db.query(Course)
    if published_only:
        q = q.filter(Course.is_published)
    if difficulty:
        q = q.filter(Course.difficulty == difficulty)

    # `tags` and `retired` both live inside JSON text columns, which SQLite and Postgres
    # do not filter alike, so both are applied in Python. The catalogue is a few dozen
    # rows; when it stops being, these become real columns.
    rows = q.order_by(Course.created_at.desc()).all()
    if not include_retired:
        rows = [c for c in rows if not qsp_paths.course_meta_of(c).get("retired")]
    if tags:
        wanted = {t for t in tags if t}
        rows = [c for c in rows if wanted <= set(_course_tags(c))]

    total = len(rows)
    return PaginatedResponse(
        items=rows[offset : offset + limit], total=total, limit=limit, offset=offset
    )


def _course_tags(course: Course) -> list[str]:
    """`course.tags` as a list, tolerating the unparseable."""
    try:
        parsed = json.loads(course.tags or "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in parsed] if isinstance(parsed, list) else []


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
        .filter(Course.id == course_id, Course.tenant_id == tenant_uuid(user))
        .first()
    )
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return course


@router.get("/{course_id}/outline")
def course_outline(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """A course with everything needed to actually read it.

    `CourseOut` returns modules but not their content, so the course page could only
    ever list module titles. This walks the teach -> check -> assess rows so the page
    can show what each module teaches, the quiz that checks it, the lab that assesses
    it, and the performance objective it satisfies.
    """
    course = (
        db.query(Course)
        .filter(Course.id == course_id, Course.tenant_id == tenant_uuid(user))
        .one_or_none()
    )
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")

    modules = (
        db.query(CourseModule)
        .filter(CourseModule.course_id == course.id)
        .order_by(CourseModule.ordinal)
        .all()
    )
    module_ids = [m.id for m in modules]

    contents: dict[str, list[ModuleContent]] = {}
    lessons: dict[str, Lesson] = {}
    quizzes: dict[str, Quiz] = {}
    if module_ids:
        rows = (
            db.query(ModuleContent)
            .filter(ModuleContent.module_id.in_(module_ids))
            .order_by(ModuleContent.ordinal)
            .all()
        )
        for row in rows:
            contents.setdefault(str(row.module_id), []).append(row)
        lesson_ids = [r.lesson_id for r in rows if r.lesson_id]
        if lesson_ids:
            # tenant-safe: reached only through this course's own modules.
            lessons = {
                str(x.id): x for x in db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).all()
            }
        quiz_ids = [r.quiz_id for r in rows if r.quiz_id]
        if quiz_ids:
            # tenant-safe: reached only through this course's own modules.
            quizzes = {
                str(x.id): x for x in db.query(Quiz).filter(Quiz.id.in_(quiz_ids)).all()
            }

    # Which performance objective each module satisfies, if any.
    po_by_module: dict[str, dict] = {}
    bound = [m for m in modules if m.po_id]
    if bound:
        for po, qual in (
            db.query(PerformanceObjective, Qualification)
            .join(Qualification, Qualification.id == PerformanceObjective.qualification_id)
            .filter(PerformanceObjective.id.in_([m.po_id for m in bound]))
            .all()
        ):
            for m in bound:
                if str(m.po_id) == str(po.id):
                    po_by_module[str(m.id)] = {
                        "qsp_code": qual.qsp_code,
                        "qualification": qual.title or qual.nqual,
                        "po_code": po.po_code,
                        "title": po.title,
                    }

    meta = qsp_paths.course_meta_of(course)
    out_modules = []
    for m in modules:
        rows = contents.get(str(m.id), [])
        teach = next((r for r in rows if r.content_kind == ContentKind.teach), None)
        check = next((r for r in rows if r.content_kind == ContentKind.check), None)
        assess = next((r for r in rows if r.content_kind == ContentKind.assess), None)
        lesson = lessons.get(str(teach.lesson_id)) if teach and teach.lesson_id else None
        quiz = quizzes.get(str(check.quiz_id)) if check and check.quiz_id else None

        lab = ""
        if assess is not None and assess.external_ref:
            try:
                lab = (json.loads(assess.external_ref) or {}).get("lab", "")
            except (TypeError, ValueError):
                lab = ""

        out_modules.append({
            "id": str(m.id),
            "ordinal": m.ordinal,
            "title": m.title,
            "content_type": m.content_type.value if m.content_type else "",
            "duration_minutes": m.duration_minutes,
            "is_required": m.is_required,
            "pass_threshold": m.pass_threshold,
            "body_markdown": lesson.body_markdown if lesson else "",
            "quiz": (
                {
                    "id": str(quiz.id),
                    "title": quiz.title,
                    "pass_pct": quiz.pass_pct,
                    "question_count": len(quiz.questions),
                }
                if quiz
                else None
            ),
            "lab": lab,
            "scenario_id": str(assess.scenario_id) if assess and assess.scenario_id else None,
            "delivers": po_by_module.get(str(m.id)),
        })

    return {
        "id": str(course.id),
        "course_code": meta.get("course_code") or "",
        "name": course.name,
        "description": course.description,
        "difficulty": course.difficulty,
        "duration_hours": course.duration_hours,
        "is_published": course.is_published,
        "institution": meta.get("institution") or "",
        "dp_order": meta.get("dp_order") or 0,
        "term_label": meta.get("term_label") or "",
        "provenance": meta.get("content_provenance") or meta.get("provenance") or "",
        "status": meta.get("content_status") or meta.get("status") or "",
        "tags": _course_tags(course),
        "modules": out_modules,
    }


@router.patch("/{course_id}", response_model=CourseOut)
def update_course(
    course_id: uuid.UUID,
    body: CourseUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Update course metadata."""
    course = get_owned(db, Course, course_id, user)
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


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_course(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a course and its modules."""
    course = get_owned(db, Course, course_id, user)
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    db.query(CourseModule).filter(CourseModule.course_id == course_id).delete()
    db.delete(course)
    db.commit()


@router.post("/import-programme")
async def import_programme(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Upload a programme catalogue CSV and upsert its courses (idempotent).

    Courses are created unpublished and, unless the row names a real ``qsp_code``,
    unbound from the qualification spine. See ``programme_ingest`` for why.
    """
    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="programme catalogue too large",
        )
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"programme catalogue must be UTF-8 CSV: {exc}") from exc
    try:
        stats = programme_ingest.import_programme(db, csv_text, tenant_id=tenant_uuid(user))
    except Exception as exc:  # noqa: BLE001 — surface parse/DB errors to the caller
        db.rollback()
        logger.exception("programme catalogue import failed")
        raise HTTPException(status_code=422, detail=f"programme import failed: {exc}") from exc
    return {"imported": True, **stats}


@router.post("/import-course-content")
async def import_course_content(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Upload an authored course YAML and attach its modules/quizzes (idempotent).

    The course must already exist in the programme catalogue. Everything created
    here is unpublished.
    """
    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="course file too large",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"course file must be UTF-8 YAML: {exc}") from exc
    try:
        stats = course_content_ingest.import_course_content(db, text, tenant_id=tenant_uuid(user))
    except Exception as exc:  # noqa: BLE001 — surface parse/DB errors to the caller
        db.rollback()
        logger.exception("course content import failed")
        raise HTTPException(status_code=422, detail=f"course content import failed: {exc}") from exc
    return {"imported": True, **stats}


@router.post("/generate-programme-paths")
def generate_programme_paths(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Build unpublished LearningPaths for the imported programme, term by term.

    Delivery schedule only. These paths carry no qualification claim and are separate
    from the CFITES developmental paths generated from the QSP spine.
    """
    try:
        stats = programme_ingest.generate_programme_paths(db, tenant_id=tenant_uuid(user))
    except Exception as exc:  # noqa: BLE001 — surface DB errors to the caller
        db.rollback()
        logger.exception("programme path generation failed")
        raise HTTPException(status_code=422, detail=f"path generation failed: {exc}") from exc
    return {"generated": True, **stats}


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
    """Enroll a user in a course.

    Enrolling someone *other* than yourself is an administrative act. This
    endpoint previously trusted ``body.user_id`` outright, so any authenticated
    user could enroll anyone else in any course in their tenant.
    """
    if str(body.user_id) != str(user.id) and not user_has_permission(user, Permission.USER_UPDATE):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Enrolling another user requires the user:update permission",
        )

    course = get_owned(db, Course, course_id, user)
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")

    existing = (
        db.query(Enrollment).filter(Enrollment.user_id == body.user_id, Enrollment.course_id == course_id).first()
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "User already enrolled in this course")

    enrollment = ensure_enrollment(
        db, user_id=body.user_id, course_id=course_id, tenant_id=user.tenant_id
    )
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
    return db.query(Enrollment).filter(Enrollment.course_id == course_id).order_by(Enrollment.enrolled_at.desc()).all()


@router.get("/{course_id}/progress/{user_id}", response_model=list[ModuleProgressOut])
def get_user_progress(
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get per-module progress for a user in a course."""
    enrollment = db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id).first()
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    return db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id).all()


@router.post("/{course_id}/complete/{user_id}", response_model=EnrollmentOut)
def complete_course(
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Mark a course enrollment as completed and calculate final grade."""
    enrollment = db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id).first()
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")

    # Calculate final score from module progress
    progress_rows = db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id).all()
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
    enrollment.completed_at = datetime.now(UTC)
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
    duplicate = (
        db.query(LearningPath)
        .filter(LearningPath.tenant_id == user.tenant_id, LearningPath.name == body.name)
        .first()
    )
    if duplicate:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A learning path named '{body.name}' already exists. Edit it or pick another name.",
        )
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
    """List the tenant's learning paths."""
    return (
        db.query(LearningPath)
        .filter(LearningPath.tenant_id == user.tenant_id)
        .order_by(LearningPath.created_at.desc())
        .all()
    )


@lp_router.get("/{lp_id}", response_model=LearningPathOut)
def get_learning_path(
    lp_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single learning path."""
    lp = get_owned(db, LearningPath, lp_id, user)
    if not lp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Learning path not found")
    return lp


@lp_router.patch("/{lp_id}", response_model=LearningPathOut)
def update_learning_path(
    lp_id: uuid.UUID,
    body: LearningPathUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a learning path."""
    lp = (
        db.query(LearningPath)
        .filter(LearningPath.id == lp_id, LearningPath.tenant_id == user.tenant_id)
        .first()
    )
    if not lp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Learning path not found")
    if body.name is not None and body.name != lp.name:
        clash = (
            db.query(LearningPath)
            .filter(
                LearningPath.tenant_id == user.tenant_id,
                LearningPath.name == body.name,
                LearningPath.id != lp_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"A learning path named '{body.name}' already exists."
            )
        lp.name = body.name
    if body.description is not None:
        lp.description = body.description
    if body.course_ids is not None:
        lp.course_ids = json.dumps([str(c) for c in body.course_ids])
    if body.is_published is not None:
        lp.is_published = body.is_published
    db.commit()
    db.refresh(lp)
    return lp


@lp_router.delete("/{lp_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_learning_path(
    lp_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a learning path."""
    lp = (
        db.query(LearningPath)
        .filter(LearningPath.id == lp_id, LearningPath.tenant_id == user.tenant_id)
        .first()
    )
    if not lp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Learning path not found")
    db.delete(lp)
    db.commit()


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
    exercises = db.query(Exercise).filter(Exercise.tenant_id == user.tenant_id).all()
    for ex in exercises:
        entries.append(
            TranscriptEntry(
                source="truenorth",
                activity_type="exercise",
                title=ex.name,
                score=ex.total_score,
                max_score=ex.max_score,
                completed_at=ex.completed_at,
            )
        )

    # 2. Course enrollments
    enrollments = db.query(Enrollment).filter(Enrollment.user_id == user_id).all()
    for enr in enrollments:
        course = get_owned(db, Course, enr.course_id, user)
        if course:
            entries.append(
                TranscriptEntry(
                    source="truenorth",
                    activity_type="course",
                    title=course.name,
                    score=enr.final_score,
                    max_score=enr.max_score,
                    grade=enr.final_grade,
                    completed_at=enr.completed_at,
                )
            )

    # 3. External activities
    ext_activities = db.query(ExternalActivity).filter(ExternalActivity.user_id == user_id).all()
    for ea in ext_activities:
        entries.append(
            TranscriptEntry(
                source=ea.activity_type,
                activity_type=ea.activity_type,
                title=ea.title,
                score=ea.score,
                max_score=ea.max_score,
                completed_at=ea.completed_at,
            )
        )

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


@lp_router.post("/{path_id}/assign-group")
def assign_learning_path_to_group(
    path_id: uuid.UUID,
    body: LearningPathGroupAssignIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
) -> dict:
    """Enrol every member of a security group on a learning path.

    **Permission: user:update**

    Security groups mirror the AD groups Keycloak federates, so this is how a
    whole cohort gets its programme in one action rather than one enrolment at a
    time. Idempotent: members already enrolled are left alone.
    """
    path = get_owned(db, LearningPath, path_id, user, not_found="Learning path not found")
    group = get_owned(db, SecurityGroup, body.group_id, user, not_found="Security group not found")

    # tenant-safe: reached only through a security group already scoped above.
    member_ids = [
        row.user_id
        for row in db.query(SecurityGroupMembership).filter(
            SecurityGroupMembership.group_id == group.id
        )
    ]
    if not member_ids:
        return {
            "learning_path_id": str(path.id),
            "group_id": str(group.id),
            "members": 0,
            "enrolled": 0,
            "note": "The group has no members. Run an AD sync if it should.",
        }

    # Members must be in the caller's tenant — a group could in principle name a
    # user from elsewhere, and enrolment writes a tenant-scoped row.
    members = (
        db.query(User)
        .filter(User.id.in_(member_ids), User.tenant_id == tenant_uuid(user))
        .all()
    )

    enrolled = 0
    for member in members:
        enrolled += len(
            ensure_path_enrollment(
                db,
                user_id=member.id,
                learning_path_id=path.id,
                tenant_id=tenant_uuid(user),
            )
        )
    db.commit()

    logger.info(
        "Assigned learning path %s to group %s (%d members, %d enrolments)",
        path.id,
        group.id,
        len(members),
        enrolled,
    )
    return {
        "learning_path_id": str(path.id),
        "group_id": str(group.id),
        "members": len(members),
        "skipped_out_of_tenant": len(member_ids) - len(members),
        "enrolled": enrolled,
    }
