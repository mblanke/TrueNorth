"""The rules of use: read, sign, and (administrators) publish and export (ADR 0014 §3; #279).

=================================================  ==============================================
Endpoint                                           Who
=================================================  ==============================================
GET  /acknowledgement/current                      any signed-in user, even one who must sign
POST /acknowledgement/sign                         any signed-in user, even one who must sign
GET  /acknowledgement/versions                     the platform administrator: drafts and versions
POST /acknowledgement/versions                     the platform administrator: a new draft
PUT  /acknowledgement/versions/{id}                the platform administrator: edit a draft
POST /acknowledgement/versions/{id}/publish        the platform administrator: publish a draft
GET  /acknowledgement/signatures/export            admins: their tenant's signatures (CSV); the
                                                   platform administrator: everyone's
=================================================  ==============================================

The first two depend on ``auth.get_signed_in_user`` (account, disabled and MFA checks, no
acknowledgement check), so someone who must sign can. They are the only routes that do;
every other one, the rest of this router included, depends on ``get_current_user`` and
answers 403 ``acknowledgement_required`` until the caller has signed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, client_ip, get_signed_in_user, require_role
from ..db import get_db
from ..models import UserRole
from ..rbac import is_platform_admin, require_platform_admin
from ..tenancy import tenant_uuid
from . import rule, service
from .schemas import AcknowledgementCurrentOut, AcknowledgementDraftIn, AcknowledgementSignIn, AcknowledgementVersionOut

router = APIRouter(prefix="/acknowledgement", tags=["acknowledgement"])

_PLATFORM_ADMIN = require_platform_admin()


def _current_out(st: rule.Status) -> AcknowledgementCurrentOut:
    return AcknowledgementCurrentOut(
        version=AcknowledgementVersionOut.model_validate(st.current) if st.current is not None else None,
        required=st.required, reason=st.reason, signed_at=st.signed_at, expires_at=st.expires_at,
    )


def _fail(exc: service.AcknowledgementError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/current", response_model=AcknowledgementCurrentOut, operation_id="getCurrentAcknowledgement",
            summary="The rules of use the caller must sign, and whether they have")
def get_current(db: Session = Depends(get_db), user: CurrentUser = Depends(get_signed_in_user)):
    return _current_out(rule.status(db, user.id, user.tenant_id))


@router.post("/sign", response_model=AcknowledgementCurrentOut, operation_id="signAcknowledgement",
             summary="Agree to the current rules of use")
def sign(body: AcknowledgementSignIn, request: Request, db: Session = Depends(get_db),
         user: CurrentUser = Depends(get_signed_in_user)):
    try:
        st = service.sign(db, user, body.version_id, ip=client_ip(request),
                          user_agent=request.headers.get("user-agent"))
    except service.AcknowledgementError as exc:
        raise _fail(exc) from exc
    db.commit()
    return _current_out(st)


@router.get("/versions", response_model=list[AcknowledgementVersionOut], operation_id="listAcknowledgementVersions",
            summary="Every draft and published version")
def list_versions(db: Session = Depends(get_db), _: CurrentUser = Depends(_PLATFORM_ADMIN)):
    return service.list_versions(db)


@router.post("/versions", response_model=AcknowledgementVersionOut, status_code=201,
             operation_id="createAcknowledgementDraft", summary="Start a new draft")
def create_draft(body: AcknowledgementDraftIn, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(_PLATFORM_ADMIN)):
    row = service.create_draft(db, user.id, body.text)
    db.commit()
    return row


@router.put("/versions/{version_id}", response_model=AcknowledgementVersionOut,
            operation_id="updateAcknowledgementDraft", summary="Change a draft (a published version never changes)")
def update_draft(version_id: uuid.UUID, body: AcknowledgementDraftIn, db: Session = Depends(get_db),
                 _: CurrentUser = Depends(_PLATFORM_ADMIN)):
    try:
        row = service.update_draft(db, version_id, body.text)
    except service.AcknowledgementError as exc:
        raise _fail(exc) from exc
    db.commit()
    return row


@router.post("/versions/{version_id}/publish", response_model=AcknowledgementVersionOut,
             operation_id="publishAcknowledgementVersion",
             summary="Publish a draft as the next version: everyone signs it before going on")
def publish(version_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(_PLATFORM_ADMIN)):
    try:
        row = service.publish(db, version_id, user.id)
    except service.AcknowledgementError as exc:
        raise _fail(exc) from exc
    db.commit()
    return row


@router.get("/signatures/export", response_class=Response, operation_id="exportAcknowledgementSignatures",
            summary="Signatures as CSV: the caller's organisation, or every one for the platform administrator",
            responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}})
def export_signatures(db: Session = Depends(get_db), user: CurrentUser = Depends(require_role(UserRole.admin))):
    platform = is_platform_admin(user, db)
    text, rows = service.export_csv(db, None if platform else tenant_uuid(user))
    service.audit_export(db, user, "all" if platform else "tenant", rows)
    db.commit()
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="acknowledgement-signatures-{stamp}.csv"'})
