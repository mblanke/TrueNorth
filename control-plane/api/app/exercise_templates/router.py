"""The exercise template's API (ADR 0017).

==========================================================  ==========================
Endpoint                                                    Permission
==========================================================  ==========================
GET    /exercise-templates                                  exercise:create
POST   /exercise-templates                                  exercise:create
GET    /exercise-templates/{id}                             exercise:create
PUT    /exercise-templates/{id}                             exercise:create
DELETE /exercise-templates/{id}                             exercise:create
POST   /exercise-templates/{id}/exercises                   exercise:create
GET    /exercise-templates/{id}/exercises                   exercise:create
POST   /exercise-templates/from-exercise/{exercise_id}      exercise:create
==========================================================  ==========================

Reads need ``exercise:create`` too (admins and instructors): the MESL and the instructor's
notes are the exercise-control plan, the answer key (as in ``collective-exercises``). Sharing
a template with every tenant (``is_public``) is the platform administrator's. Whether a
template is proven is ``GET /content-port/items/exercise_template/{id}/proven``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from . import service
from .models import ExerciseTemplate
from .schemas import (
    ExerciseFromTemplateIn,
    ExerciseFromTemplateOut,
    ExerciseMadeFromTemplateOut,
    ExerciseSaveAsTemplateIn,
    ExerciseTemplateIn,
    ExerciseTemplateListOut,
    ExerciseTemplateOut,
    ExerciseTemplateUpdate,
)

router = APIRouter(prefix="/exercise-templates", tags=["exercise-templates"])
_STAFF = require_permission(Permission.EXERCISE_CREATE)


@router.get("", response_model=list[ExerciseTemplateListOut], operation_id="list_exercise_templates",
            summary="Exercise templates of this tenant and the shared ones")
def list_templates(db: Session = Depends(get_db), user: CurrentUser = Depends(_STAFF)) -> list[ExerciseTemplateListOut]:
    rows = service.visible_query(db, user).order_by(ExerciseTemplate.name, ExerciseTemplate.id).all()
    return [service.list_out(r, user) for r in rows]


@router.post("", response_model=ExerciseTemplateOut, status_code=201, operation_id="create_exercise_template",
             summary="Create an exercise template")
def create_template(body: ExerciseTemplateIn, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(_STAFF)) -> ExerciseTemplateOut:
    row = service.create(db, user, body)
    db.commit()
    return service.out(row, user)


@router.post("/from-exercise/{exercise_id}", response_model=ExerciseTemplateOut, status_code=201,
             operation_id="save_exercise_as_template",
             summary="Save an existing exercise as a template (the exercise is not changed)")
def save_as_template(body: ExerciseSaveAsTemplateIn, exercise_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                     user: CurrentUser = Depends(_STAFF)) -> ExerciseTemplateOut:
    row = service.save_exercise(db, user, exercise_id, body)
    db.commit()
    return service.out(row, user)


@router.get("/{template_id}", response_model=ExerciseTemplateOut, operation_id="get_exercise_template",
            summary="One exercise template (this tenant's, or a shared one)")
def get_template(template_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                 user: CurrentUser = Depends(_STAFF)) -> ExerciseTemplateOut:
    return service.out(service.get_visible(db, user, template_id), user)


@router.put("/{template_id}", response_model=ExerciseTemplateOut, operation_id="update_exercise_template",
            summary="Edit an exercise template (never changes exercises already made from it)")
def update_template(body: ExerciseTemplateUpdate, template_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                    user: CurrentUser = Depends(_STAFF)) -> ExerciseTemplateOut:
    row = service.update(db, user, template_id, body)
    db.commit()
    return service.out(row, user)


@router.delete("/{template_id}", status_code=204, response_class=Response, operation_id="delete_exercise_template",
               summary="Delete an exercise template no exercise was made from",
               responses={409: {"description": "Exercises were made from it"}})
def delete_template(template_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                    user: CurrentUser = Depends(_STAFF)) -> Response:
    service.delete(db, user, template_id)
    db.commit()
    return Response(status_code=204)


@router.post("/{template_id}/exercises", response_model=ExerciseFromTemplateOut, status_code=201,
             operation_id="create_exercise_from_template",
             summary="Create an exercise from a template: its objectives and MESL are copied")
def create_exercise(body: ExerciseFromTemplateIn, template_id: uuid.UUID = Path(...),
                    db: Session = Depends(get_db), user: CurrentUser = Depends(_STAFF)) -> ExerciseFromTemplateOut:
    out = service.create_exercise(db, user, template_id, body)
    db.commit()
    return out


@router.get("/{template_id}/exercises", response_model=list[ExerciseMadeFromTemplateOut],
            operation_id="list_exercises_from_template",
            summary="This tenant's exercises made from a template, with the revision each came from")
def list_exercises(template_id: uuid.UUID = Path(...), db: Session = Depends(get_db),
                   user: CurrentUser = Depends(_STAFF)) -> list[ExerciseMadeFromTemplateOut]:
    return service.exercises_of(db, user, template_id)
