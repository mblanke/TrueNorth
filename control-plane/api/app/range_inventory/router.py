"""Range inventory checks and the confirm-by-moid of unidentified items (ADR 0013 §5, #243).

=====================================================  =================================
Endpoint                                               Permission
=====================================================  =================================
GET  /ranges/{range_id}/inventory-checks                range:certification_read (staff)
POST /ranges/{range_id}/teardown/confirm-unidentified  range:admin, each moid typed back,
                                                       with a reason
=====================================================  =================================

Another tenant's range is 404; a Student lab's range is 404 to staff without
infrastructure rights, as for its certification record.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..range_certification.service import has_in_flight, readable_range
from ..range_ops import service as ops
from ..rbac import Permission, require_permission
from . import service
from .schemas import ConfirmUnidentifiedIn, ConfirmUnidentifiedOut, RangeInventoryChecksOut

logger = logging.getLogger("truenorth.api.range_inventory")

router = APIRouter(prefix="/ranges", tags=["ranges"])
_ERRORS: dict = {
    403: {"description": "Not staff (read), or not an administrator (confirm)"},
    404: {"description": "No such range in the caller's tenant"},
}


@router.get("/{range_id}/inventory-checks", response_model=RangeInventoryChecksOut, responses=_ERRORS,
            summary="The range's last teardown verification and drift check")
def get_inventory_checks(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_CERTIFICATION_READ)),
) -> RangeInventoryChecksOut:
    """What the range's last teardown deleted by recorded id, which recorded ids remain, and
    what carries the range's mark but was not deleted (unidentified); and the latest drift
    check of the live range against its recorded inventory (VM ids, CPU/RAM, NICs, port
    group VLANs, snapshots). **Permission: range:certification_read**"""
    rng = readable_range(db, range_id, user)
    if has_in_flight(db, rng):  # a destroy the worker finished is settled first, as on the record's read
        ops.reconcile(db, rng)
        db.commit()
    return service.view(db, rng, user)


@router.post("/{range_id}/teardown/confirm-unidentified", response_model=ConfirmUnidentifiedOut, status_code=202,
             responses={**_ERRORS, 409: {"description": "The range is not torn down, is busy, or lists nothing"},
                        422: {"description": "An item is not listed, or its typed id does not match exactly"}},
             summary="Confirm unidentified items by their typed-back moid, to be deleted (range:admin)")
def confirm_unidentified(
    body: ConfirmUnidentifiedIn,
    response: Response,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_ADMIN)),
) -> ConfirmUnidentifiedOut:
    """DANGEROUS. Delete items the range's last teardown listed as unidentified (they carry
    its annotation, tag or owner mark but were not recorded at clone). Each item's
    ``confirm_moid`` must repeat its ``moid`` exactly, and the moid must be in the listed
    set; otherwise nothing is confirmed (422). Audit-logged with the reason and recorded on
    the range's certification record as an override. The worker re-checks each item against
    vCenter before deleting it, and re-verifies the teardown.
    **Permission: range:admin** (admin only)"""
    rng = readable_range(db, range_id, user)
    try:
        accepted = service.confirm(db, rng, user, body)
        db.commit()
    except HTTPException:
        raise  # refused before anything was written
    except Exception:
        db.rollback()
        raise
    task_id = service.send(rng, user, accepted)
    logger.warning("range %s: %d unidentified item(s) confirmed for deletion by %s: %s", rng.id, len(accepted),
                   user.id, body.reason)
    if task_id:
        response.headers["Task-Id"] = task_id
    return ConfirmUnidentifiedOut(
        range_id=rng.id, accepted=[service.item_out(i) for i in accepted], task_id=task_id,
        dispatched=task_id is not None,
        message="Queued: each item is checked against vCenter again, then deleted; the teardown is re-verified."
        if task_id else "Recorded, but not queued (the task broker is unavailable); confirm again shortly.")
