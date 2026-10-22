"""The Range Certification Record endpoints (ADR 0013 §6, issue #244).

==============================================  ==========================================
Endpoint                                        Permission(s)
==============================================  ==========================================
GET  /ranges/{range_id}/certification           range:certification_read (admin,
GET  /ranges/{range_id}/certification/export    instructor, range_ops); Students and
GET  /ranges/{range_id}/certification/export/pdf  observers 403; another tenant's range 404
POST /ranges/{range_id}/certification/capture   range:certification_read + range:provision
POST /ranges/{range_id}/certification/          range:certification_read + range:provision
     isolation-check (S4 re-probe; 409 unless ready)
GET  /range-certification/signing-keys          range:certification_read
==============================================  ==========================================

Archived records (earlier builds) can be exported until retention purges them
(retention.py). Exports are audited. Reads write nothing in the usual case: only when an
operation of the range is in flight do they settle it (as GET /ranges/{id}/operations
does; that is when a finished build's or destroy's evidence is appended), and only when
the build's evidence differs from the record's do they append it (no lock otherwise).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..range_ops import service as ops
from ..rbac import Permission, require_permission, user_has_permission
from . import pdf, service
from .retention import retention_months
from .schemas import (
    CertificationCaptureOut,
    CertificationExportOut,
    CertificationRecordOut,
    IsolationCheckOut,
    RangeCertificationOut,
    SigningKeyOut,
)

router = APIRouter(prefix="/ranges", tags=["ranges"])
_READ = require_permission(Permission.RANGE_CERTIFICATION_READ)
_ERRORS: dict = {
    403: {"description": "Not staff: Students and observers do not see certification records"},
    404: {"description": "No such range in the caller's tenant, or no record for that deployment"},
}
_DEPLOYMENT = Query(None, description="A deployment (provision operation) id; the newest when omitted")


def _settled(db: Session, range_id: uuid.UUID, user: CurrentUser):
    rng = service.readable_range(db, range_id, user)
    wrote = False
    if service.has_in_flight(db, rng):
        ops.reconcile(db, rng)  # a finished build or destroy appends its evidence here
        wrote = True
    wrote = service.refresh_if_changed(db, rng) or wrote  # a check the worker settled since
    if wrote:
        db.commit()
    return rng


@router.get("/{range_id}/certification", response_model=RangeCertificationOut, responses=_ERRORS)
def get_range_certification(
    range_id: uuid.UUID = Path(...),
    deployment_id: uuid.UUID | None = _DEPLOYMENT,
    history: bool = Query(False, description="Every entry of each section, superseded ones too (default: latest)"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> RangeCertificationOut:
    """The range's current certification record (its newest build's, unless a deployment
    is named), with its verdict: certified / ready_not_certified (reasons) / not_safe
    (failures), each section's latest entry (``history=1``: all of them), and the archived
    records of earlier builds. **Permission: range:certification_read**"""
    rng = _settled(db, range_id, user)
    record = service.chosen_record(db, rng, deployment_id)
    view = service.view(db, record, history=history) if record else None
    return RangeCertificationOut(
        range_id=rng.id,
        record=CertificationRecordOut.model_validate(view) if view else None,
        archived=service.archived(db, rng),
        retention_months=retention_months(),
    )


def _signed(db: Session, range_id: uuid.UUID, deployment_id: uuid.UUID | None, user: CurrentUser, what: str):
    rng = _settled(db, range_id, user)
    record = service.chosen_record(db, rng, deployment_id)
    if record is None:
        raise HTTPException(404, "This range has no certification record yet")
    rec, record_sha = service.record_document(db, record)
    payload = {
        "format": "truenorth.range-certification.v1",
        "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "exported_by": service.actor_of(user)[1],
        "record_sha256": record_sha,
        "record": rec,
    }
    signed = service.sign_export(db, payload)
    service.audit(db, user, rng, "range_certification_export",
                  f"{what}; deployment={record.deployment_id}; record_sha256={payload['record_sha256']}; "
                  f"signed_sha256={signed['signature']['signed_sha256']}")
    db.commit()
    return record, signed


@router.get("/{range_id}/certification/export", response_model=CertificationExportOut, responses=_ERRORS)
def export_range_certification(
    range_id: uuid.UUID = Path(...),
    deployment_id: uuid.UUID | None = _DEPLOYMENT,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> JSONResponse:
    """The record as signed JSON (Ed25519; app/range_certification/signing.py). The
    signature covers the canonical JSON of everything but ``signature``.
    **Permission: range:certification_read**. Audited."""
    record, signed = _signed(db, range_id, deployment_id, user, "json")
    return JSONResponse(signed, headers={
        "Content-Disposition": f'attachment; filename="range-certification-{record.deployment_id}.json"',
        "Cache-Control": "no-store"})


@router.get(
    "/{range_id}/certification/export/pdf",
    response_class=Response,
    responses={**_ERRORS, 200: {"content": {"application/pdf": {}}, "description": "The record as a PDF"}},
)
def export_range_certification_pdf(
    range_id: uuid.UUID = Path(...),
    deployment_id: uuid.UUID | None = _DEPLOYMENT,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> Response:
    """A readable PDF of the record, naming the signed JSON's SHA-256 and key id (the JSON
    is what verifies). **Permission: range:certification_read**. Audited."""
    record, signed = _signed(db, range_id, deployment_id, user, "pdf")
    return Response(pdf.render(signed), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="range-certification-{record.deployment_id}.pdf"',
        "Cache-Control": "no-store"})


@router.post(
    "/{range_id}/certification/capture",
    response_model=CertificationCaptureOut,
    responses={**_ERRORS, 409: {"description": "The range was never provisioned through TrueNorth"}},
)
def capture_range_certification(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> CertificationCaptureOut:
    """Append the evidence TrueNorth holds now (guest readiness, network allocation,
    template names) to the newest deployment's record, where it changed. Opens the record
    of a build accepted before records began. Appends only; never edits.
    **Permission: range:certification_read and range:provision**"""
    if not user_has_permission(user, Permission.RANGE_PROVISION):
        raise HTTPException(403, "Recording evidence needs range:provision")
    rng = _settled(db, range_id, user)
    record = service.open_for_latest_build(db, rng, user)
    failed = service.deployment_status(db, record) == "failed"
    written = service.capture_build(db, rng, record, rng.error_message if failed else None)
    service.audit(db, user, rng, "range_certification_capture", f"deployment={record.deployment_id}; written={written}")
    db.commit()
    return CertificationCaptureOut(deployment_id=record.deployment_id, written=written)


@router.post(
    "/{range_id}/certification/isolation-check",
    response_model=IsolationCheckOut,
    status_code=202,
    responses={**_ERRORS, 409: {"description": "The range is not ready (its VMs must be up to be probed)"}},
)
def check_range_isolation(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> IsolationCheckOut:
    """Probe the range's isolation again from every zone (ADR 0013 §4: the worker runs the
    probes in each zone's probe VM through guest operations and stores the result; the
    record appends it on its next read). Use it after the network around the range
    changed. It never changes the range's state, in warn or enforce mode. Audited.
    **Permission: range:certification_read and range:provision**"""
    if not user_has_permission(user, Permission.RANGE_PROVISION):
        raise HTTPException(403, "Probing a range needs range:provision")
    rng = service.readable_range(db, range_id, user)
    if getattr(rng.state, "value", rng.state) != "ready":
        raise HTTPException(409, "Only a ready range can be probed")
    from ..celery_client import dispatch

    dispatched = dispatch("check_range_isolation", str(rng.id)) is not None
    service.audit(db, user, rng, "range_isolation_check", f"dispatched={dispatched}")
    db.commit()
    return IsolationCheckOut(range_id=rng.id, dispatched=dispatched)


keys_router = APIRouter(prefix="/range-certification", tags=["ranges"])


@keys_router.get("/signing-keys", response_model=list[SigningKeyOut])
def list_certification_signing_keys(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> list[SigningKeyOut]:
    """Every public key that has signed certification exports, the current one first and
    marked, retired ones kept (an export made before a TN_SECRETS_KEY rotation still
    verifies against its key_id here). Record the current key's id out of band to trust it.
    How to verify: docs/range-certification.md. **Permission: range:certification_read**"""
    return [SigningKeyOut.model_validate(k) for k in service.signing_keys(db)]
