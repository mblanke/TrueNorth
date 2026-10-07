"""TrueNorth Range — Greyspace (simulated internet) router. ADR 0007.

=========================================  ==============
Endpoint                                   Permission
=========================================  ==============
GET    /greyspace/corpora                  RANGE_READ
GET    /ranges/{range_id}/greyspace        RANGE_READ
PUT    /ranges/{range_id}/greyspace        RANGE_UPDATE
DELETE /ranges/{range_id}/greyspace        RANGE_UPDATE
GET    /ranges/{range_id}/greyspace/config RANGE_READ
=========================================  ==============

Every range lookup is tenant-scoped (404 for another tenant's range). The logic is in
app/greyspace/service.py; this file only maps HTTP onto it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..greyspace import service
from ..greyspace.schemas import CorpusSummary, GreyspaceBlock, GreyspaceConfigOut, GreyspaceStatusOut
from ..rbac import Permission, require_permission

router = APIRouter(tags=["greyspace"])

_ERRORS = {
    404: {"description": "Range not found in your tenant, or no Greyspace block attached"},
    409: {"description": "The range is changing, belongs to a lab session, or its corpus is not readable here"},
    422: {"description": "The block cannot run on its corpus; the body lists the problems"},
}


@router.get("/greyspace/corpora", response_model=list[CorpusSummary], operation_id="greyspace_list_corpora")
def list_corpora(user: CurrentUser = Depends(require_permission(Permission.RANGE_READ))) -> list[CorpusSummary]:
    """The corpus tiers (T0 CI fixture, T1 Mac sample, T2 lab, full) and what the control
    plane can read of each one's manifest."""
    return service.list_corpora()


@router.get(
    "/ranges/{range_id}/greyspace",
    response_model=GreyspaceStatusOut,
    responses={404: _ERRORS[404]},
    operation_id="greyspace_get_status",
)
def get_status(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> GreyspaceStatusOut:
    """The range's Greyspace block and its status (``not_attached`` when there is none)."""
    return service.status(db, service.readable_range(db, range_id, user))


@router.put(
    "/ranges/{range_id}/greyspace",
    response_model=GreyspaceStatusOut,
    responses=_ERRORS,
    operation_id="greyspace_attach",
)
def attach(
    range_id: uuid.UUID = Path(...),
    block: GreyspaceBlock | None = Body(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> GreyspaceStatusOut:
    """Attach a Greyspace block to the range, or replace the one it has. With no body, the
    block the range's template declares is used, else the defaults (T0 corpus, all packs)."""
    return service.attach(db, service.changeable_range(db, range_id, user), block, user)


@router.delete(
    "/ranges/{range_id}/greyspace",
    status_code=204,
    response_class=Response,
    responses={404: _ERRORS[404], 409: _ERRORS[409]},
    operation_id="greyspace_detach",
)
def detach(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> Response:
    """Detach the range's Greyspace block. The shared corpus is untouched."""
    service.detach(db, service.changeable_range(db, range_id, user), user)
    return Response(status_code=204)


@router.get(
    "/ranges/{range_id}/greyspace/config",
    response_model=GreyspaceConfigOut,
    responses={404: _ERRORS[404], 409: _ERRORS[409]},
    operation_id="greyspace_get_config",
)
def get_config(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> GreyspaceConfigOut:
    """What the range's Greyspace stack is generated to be: address plan, ISPs, services,
    DNS zones and the generated file list."""
    return service.rendered_config(db, service.readable_range(db, range_id, user))
