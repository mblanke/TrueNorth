"""Platform-operator API for result-transfer public-key trust enrollment."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog
from ..rbac import require_platform_admin
from ..result_transfer.contract import ResultTransferError
from ..result_transfer.models import ResultTransferTrustKey
from ..result_transfer.trust import enroll_trusted_key, retire_trusted_key

router = APIRouter(prefix="/result-transfer/trust-keys", tags=["result-transfer"])
platform_admin = require_platform_admin()
Purpose = Literal["result-signing", "subject-signing", "receipt-signing", "result-recipient"]


class TrustKeyCreate(BaseModel):
    purpose: Purpose
    remote_enclave: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    public_key: str = Field(min_length=44, max_length=44)
    not_before: datetime | None = None
    not_after: datetime | None = None


class TrustKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: str
    remote_enclave: str
    key_id: str
    public_key: str
    active: bool
    not_before: datetime
    not_after: datetime | None
    created_at: datetime
    retired_at: datetime | None


def _audit(db: Session, user: CurrentUser, action: str, row: ResultTransferTrustKey) -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            tenant_id=uuid.UUID(user.tenant_id),
            action=action,
            resource_type="result_transfer_trust_key",
            resource_id=str(row.id),
            detail=f"purpose={row.purpose} remote_enclave={row.remote_enclave} key_id={row.key_id}",
        )
    )


@router.get("", response_model=list[TrustKeyOut])
def list_trust_keys(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(platform_admin),  # noqa: ARG001 - authorization dependency
):
    """List public trust anchors for the whole deployment. Platform administrator only."""
    return (
        db.query(ResultTransferTrustKey)
        .order_by(ResultTransferTrustKey.remote_enclave, ResultTransferTrustKey.purpose, ResultTransferTrustKey.key_id)
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.post("", response_model=TrustKeyOut, status_code=status.HTTP_201_CREATED)
def create_trust_key(
    body: TrustKeyCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(platform_admin),
):
    """Enroll public material only; exact retries return the existing trust anchor."""
    try:
        row, created = enroll_trusted_key(db, **body.model_dump())
    except ResultTransferError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if created:
        _audit(db, user, "result_transfer_trust_enroll", row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/{trust_key_id}/retire", response_model=TrustKeyOut)
def retire_trust_key_route(
    trust_key_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(platform_admin),
):
    """Retire a trust anchor without erasing its audit history."""
    existing = db.query(ResultTransferTrustKey).filter(ResultTransferTrustKey.id == trust_key_id).one_or_none()
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trust key not found")
    was_active = existing.active
    try:
        row = retire_trusted_key(
            db,
            purpose=existing.purpose,
            remote_enclave=existing.remote_enclave,
            key_id=existing.key_id,
        )
    except ResultTransferError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if was_active:
        _audit(db, user, "result_transfer_trust_retire", row)
    db.commit()
    db.refresh(row)
    return row
