# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""The Students page (``student_progress``).

=======================================  ==================================================
Endpoint                                 Who
=======================================  ==================================================
GET /students                            readiness:read + learning_record:read (instructors,
                                         admins): every Student of their own tenant, with
                                         formation, proficiency, attendance, results and
                                         qualifications. Filter by unit, search by name
GET /students/{user_id}                  the same: one Student's record, competency by NICE
                                         category and every class
=======================================  ==================================================

The tenant is always the caller's own (``tenancy.tenant_uuid``). Observers (readiness only),
Students and range ops are refused (403). A user id that is not a Student of the caller's
tenant is 404, never 403, so ids cannot be probed across tenants. Reads only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import student_progress
from .student_progress_schemas import StudentListOut, StudentRecordOut

router = APIRouter(prefix="/students", tags=["students"])

# Class data (readiness) and personal learning records: both, so observers are refused.
_READ = require_permission(Permission.READINESS_READ, Permission.LEARNING_RECORD_READ)


@router.get(
    "",
    response_model=StudentListOut,
    operation_id="listStudentProgress",
    summary="Every Student of the organisation with their progress",
)
def list_students(
    unit: str | None = Query(None, max_length=255, description="Only this formation (case and spaces ignored)"),
    q: str | None = Query(None, max_length=255, description="Search name, email and callsign"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> StudentListOut:
    """The caller's own tenant only. Results are pass, fail or incomplete; never a score.
    **readiness:read + learning_record:read**"""
    return student_progress.list_students(db, tenant_uuid(user), unit, q)


@router.get(
    "/{user_id}",
    response_model=StudentRecordOut,
    operation_id="getStudentProgress",
    summary="One Student's record: competency by NICE category and every class",
)
def get_student(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> StudentRecordOut:
    """404 unless a Student of the caller's own tenant.  **readiness:read + learning_record:read**"""
    record = student_progress.student_record(db, tenant_uuid(user), user_id)
    if record is None:
        raise HTTPException(404, "Student not found")
    return record
