"""Create identity-free local learner accounts from imported subject credentials."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..models import User, UserRole
from ..settings import UnsafeSettingsError, classification_level, enclave_id
from .contract import ResultTransferError
from .identity_backends import (
    BasePseudonymousIdentityBackend,
    OneTimeLogin,
    PseudonymousIdentity,
)
from .models import ProvisionedSubjectCredential

SUBJECT_SOURCE = "subject"


@dataclass(frozen=True)
class ProvisionedPseudonymousAccount:
    credential: ProvisionedSubjectCredential
    user: User
    identity: PseudonymousIdentity


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


def create_and_assign_account(
    db: Session,
    *,
    credential_id: uuid.UUID,
    tenant_id: uuid.UUID,
    identity_backend: BasePseudonymousIdentityBackend,
    now: datetime | None = None,
) -> ProvisionedPseudonymousAccount:
    """Create a disabled IdP identity and bind it to one imported credential.

    No password exists at this stage. The caller must commit the database transaction
    before invoking ``deliver_one_time_login`` through an approved separate channel.
    """
    identity = identity_backend.create_disabled_account()
    try:
        credential, user = assign_account(
            db,
            credential_id=credential_id,
            tenant_id=tenant_id,
            keycloak_subject=identity.subject,
            now=now,
        )
    except Exception:
        try:
            identity_backend.delete_account(identity.subject)
        except Exception as cleanup_exc:
            raise ResultTransferError(
                "pseudonymous account assignment failed and identity cleanup could not be confirmed"
            ) from cleanup_exc
        raise
    return ProvisionedPseudonymousAccount(credential=credential, user=user, identity=identity)


def _assigned_account(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    states: tuple[str, ...],
) -> tuple[ProvisionedSubjectCredential, User]:
    try:
        if classification_level() != "unclassified":
            raise ResultTransferError("pseudonymous subject accounts exist only on Unclassified")
        local_enclave = enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc
    credential = (
        db.query(ProvisionedSubjectCredential)
        .filter(
            ProvisionedSubjectCredential.tenant_id == tenant_id,
            ProvisionedSubjectCredential.destination_enclave == local_enclave,
            ProvisionedSubjectCredential.user_id == user_id,
            ProvisionedSubjectCredential.state.in_(states),
        )
        .with_for_update()
        .one_or_none()
    )
    user = db.query(User).filter(User.id == user_id, User.tenant_id == tenant_id).one_or_none()
    if credential is None or user is None or _profile_problem(user, tenant_id):
        raise ResultTransferError("pseudonymous account was not found in this tenant")
    return credential, user


def deliver_one_time_login(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_backend: BasePseudonymousIdentityBackend,
    deliver: Callable[[OneTimeLogin], None],
) -> str:
    """Rotate and deliver a temporary login, enabling the account only afterwards.

    This is both initial activation and recovery. The delivery implementation is injected:
    this module deliberately cannot choose email, screen display, print, or another channel.
    """
    _, user = _assigned_account(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        states=("assigned",),
    )
    if not user.is_active:
        raise ResultTransferError("revoked pseudonymous account cannot be recovered")
    login = identity_backend.prepare_one_time_login(user.keycloak_id)
    try:
        deliver(login)
    except Exception as exc:
        try:
            identity_backend.disable_account(user.keycloak_id)
        except Exception as disable_exc:
            raise ResultTransferError(
                "one-time login delivery failed and account disable could not be confirmed"
            ) from disable_exc
        raise ResultTransferError("one-time login delivery failed; account remains disabled") from exc
    identity_backend.enable_account(user.keycloak_id)
    return login.username


def revoke_account(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_backend: BasePseudonymousIdentityBackend,
) -> tuple[ProvisionedSubjectCredential, User]:
    """Disable a pseudonymous identity and make its subject token unusable locally."""
    credential, user = _assigned_account(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        states=("assigned", "revoked"),
    )
    identity_backend.disable_account(user.keycloak_id)
    user.is_active = False
    credential.state = "revoked"
    db.flush()
    return credential, user


def subject_token_for_user(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    """Resolve an assigned local learner to the opaque token used in result packages.

    Credential expiry limits initial account assignment. Once assigned, the stable token
    remains usable for that learner's training results until the account is revoked.
    """
    try:
        if classification_level() != "unclassified":
            raise ResultTransferError("pseudonymous subject tokens are readable only on Unclassified")
        local_enclave = enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc
    credential = (
        db.query(ProvisionedSubjectCredential)
        .filter_by(
            tenant_id=tenant_id,
            destination_enclave=local_enclave,
            user_id=user_id,
            state="assigned",
        )
        .one_or_none()
    )
    if credential is None:
        raise ResultTransferError("assigned subject credential was not found for this learner")
    return credential.opaque_subject_id
