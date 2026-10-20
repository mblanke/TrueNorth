"""GET /class-setup/mine: the courses you teach and the exercises you run (#200).

The web app reads it to show an instructor the enrol, withdraw and team controls on the
classes that are theirs. Presentation only: every write re-checks ``scope`` itself.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import Course, Exercise
from ..rbac import Permission, require_any_permission
from ..tenancy import tenant_uuid
from . import scope

router = APIRouter(prefix="/class-setup", tags=["class-setup"])


class ClassItemOut(BaseModel):
    id: uuid.UUID
    name: str


class MyClassesOut(BaseModel):
    """Taught courses and run exercises, by name. Each list is sorted by name."""

    courses: list[ClassItemOut]
    exercises: list[ClassItemOut]


@router.get("/mine", response_model=MyClassesOut)
def my_classes(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_any_permission(Permission.ENROLLMENT_MANAGE_OWN, Permission.TEAM_MANAGE_OWN)
    ),
) -> MyClassesOut:
    """The courses you teach and the exercises you run: your own upcoming or running booking
    (not ended; not draft, cancelled or completed) in your tenant names you its instructor
    (app/class_setup/scope.py). A change is still refused (409) while another instructor's
    session of the same course or exercise is running.

    **Permission: enrollment:manage_own or team:manage_own**
    """
    tid = tenant_uuid(user)
    course_ids = scope.taught_course_ids(db, user)
    exercise_ids = scope.run_exercise_ids(db, user)
    courses = (
        db.query(Course.id, Course.name)
        .filter(Course.id.in_(course_ids), (Course.tenant_id == tid) | Course.tenant_id.is_(None))
        .order_by(Course.name)
        .all()
        if course_ids
        else []
    )
    exercises = (
        db.query(Exercise.id, Exercise.name)
        .filter(Exercise.id.in_(exercise_ids), Exercise.tenant_id == tid, Exercise.deleted_at.is_(None))
        .order_by(Exercise.name)
        .all()
        if exercise_ids
        else []
    )
    return MyClassesOut(
        courses=[ClassItemOut(id=i, name=n) for i, n in courses],
        exercises=[ClassItemOut(id=i, name=n) for i, n in exercises],
    )
