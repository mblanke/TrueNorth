"""Create identity-free local learner accounts from imported subject credentials."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..models import User, UserRole
from ..settings import UnsafeSettingsError, classification_level, enclave_id
from .contract import ResultTransferError
from .models import ProvisionedSubjectCredential

SUBJECT_SOURCE = "subject"


def _profile_problem(user: User, tenant_id: uuid.UUID) -> str | None:
    if user.tenant_id != tenant_id or user.role != UserRole.student or user.source != SUBJECT_SOURCE:
        return "account is not an imported-subject Student in the credential tenant"
    if not user.email.endswith("@subjects.invalid") or not user.display_name.startswith("Learner "):
        return "account does not use the pseudonymous learner profile"
    prohibited = (
        user.first_name,
        user.last_name,
        user.rank,
        user.service_branch,
        user.unit,
        user.callsign,
        user.nation_id,
        user.ad_object_guid,
        user.ad_distinguished_name,
    )
    if any(value is not None for value in prohibited):
        return "pseudonymous account contains prohibited identity attributes"
    return None


def assign_account(
    db: Session,
    *,
    credential_id: uuid.UUID,
    tenant_id: uuid.UUID,
    keycloak_subject: str,
    now: datetime | None = None,
) -> tuple[ProvisionedSubjectCredential, User]:
    """Assign one available credential to a new identity-free local Student."""
    try:
        if classification_level() != "unclassified":
            raise ResultTransferError("pseudonymous subject accounts exist only on Unclassified")
        local_enclave = enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc
    try:
        keycloak_uuid = uuid.UUID(keycloak_subject)
    except (ValueError, AttributeError) as exc:
        raise ResultTransferError("keycloak_subject must be the UUID of a local pseudonymous account") from exc
    keycloak_subject = str(keycloak_uuid)
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ResultTransferError("assignment time must include a timezone")
    credential = (
        db.query(ProvisionedSubjectCredential)
        .filter_by(
            credential_id=credential_id,
            tenant_id=tenant_id,
            destination_enclave=local_enclave,
            state="available",
        )
        .with_for_update()
        .one_or_none()
    )
    if credential is None:
        raise ResultTransferError("available subject credential was not found in this tenant")
    expires = credential.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= current:
        raise ResultTransferError("subject credential has expired")
    if keycloak_subject == str(credential.opaque_subject_id):
        raise ResultTransferError("keycloak_subject must not reuse the cross-enclave subject token")
    if db.query(User.id).filter(User.keycloak_id == keycloak_subject).first():
        raise ResultTransferError("keycloak_subject is already assigned")

    account_id = uuid.uuid4()
    user = User(
        id=account_id,
        keycloak_id=keycloak_subject,
        email=f"subject-{account_id.hex}@subjects.invalid",
        display_name=f"Learner {account_id.hex[:8].upper()}",
        role=UserRole.student,
        tenant_id=tenant_id,
        source=SUBJECT_SOURCE,
        clearance_level="unclassified",
        is_active=True,
    )
    if problem := _profile_problem(user, tenant_id):
        raise ResultTransferError(problem)
    db.add(user)
    db.flush()
    credential.user_id = user.id
    credential.state = "assigned"
    credential.assigned_at = current
    db.flush()
    return credential, user
