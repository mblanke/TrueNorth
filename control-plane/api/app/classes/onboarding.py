"""Roll call is approval (ADR 0014 §1): a roll-call Student's first AD sign-in admits them.

Called by ``GET /auth/me`` for a valid token that has no ``users`` row. When it admits
someone, they skip Request access: the account is created as a Student in the class's
tenant through registration's own approval path (``_approve_one``, so its adoption rules
apply unchanged: never a staff row, never another tenant's row), they are enrolled on the
class's course, the entry becomes ``signed_in`` and an audit row records it.

What is trusted, and why (anti-spoofing):

1. **Only an IdP token.** An LTI session hand-off or the in-process harness
   (``tn_session`` set) never admits: an LTI platform asserts whatever email it likes.
2. **Only a verified email from the directory.** ``email_verified`` must be true, and
   the token must carry ``ad_object_guid``, which only the AD (LDAP) federation's mapper
   sets. Keycloak marks LDAP emails verified (``trustEmail``) because AD admins set them;
   an account made in Keycloak itself has no ``ad_object_guid``, and self-registration is
   off. So the email is the one AD holds, not one the user typed. Without both claims,
   Request access is the way in, as before.
   The address must be plain ASCII: Unicode look-alikes that lower-case onto ASCII
   (the Kelvin sign) never match.
3. **Exact, case-insensitive match** on the whole address, against entries still
   ``invited`` on an open class that has not ended. Removed entries, cancelled or
   finished classes, and entries already claimed by an account that has signed in never
   match.
4. **One tenant or none.** If the email is on roll calls in two tenants, nothing is
   guessed: the person goes to Request access and an admin decides.
5. **The auth zone still applies** (the registration zone's network rule, as
   ``POST /registration`` applies it).
6. **Only a roster Student is ever adopted.** If the address (ignoring case, any tenant,
   deleted or deactivated included) already names an account, it must be exactly one
   CSV-imported, active Student of the class's tenant; otherwise nothing is admitted.
   The request is built afresh from directory claims: nothing the person typed into
   Request access (rank, unit, a qualification) is approved by the roll call.
7. **REGISTRATION_ALLOWED_GROUPS does not apply**: the roll call is the approval (owner
   decision, ADR 0014 §1).
8. **The approver is the class's lead**, who must still be an active instructor or admin
   of the class's tenant; ``_approve_one`` re-checks that they could grant the Student
   role in that tenant. Any refusal there (an email that is a staff account, an account
   in another tenant) rolls back and falls back to Request access.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import identity as ident
from ..auth import CurrentUser, TokenPayload
from ..enrollment import ensure_enrollment
from ..models import AuditLog, RegistrationRequest, RegistrationStatus, User, UserRole
from .models import Class, ClassStatus, RollCallEntry, RollCallStatus
from .service import LEAD_ROLES, accounts_with_email, is_pre_provisioned, normalise_email

logger = logging.getLogger("truenorth.api.classes.onboarding")


def trusted_email(identity: TokenPayload) -> str | None:
    """The token's email if it may be matched to a roll call (rules 1 and 2), else None.

    ASCII only: ``str.lower`` folds look-alikes such as the Kelvin sign (U+212A) onto ASCII
    letters, so "\u212aate@corp.test" would otherwise match a roll call's kate@corp.test."""
    if identity.tn_session:
        return None
    if identity.email_verified is not True:
        return None
    if not (identity.ad_object_guid or "").strip():
        return None
    raw = (identity.email or "").strip()
    if not raw or not raw.isascii():
        return None
    return normalise_email(raw)


def _adoptable_roster_row(u: User, tenant_id) -> bool:
    """A roster row a roll-call sign-in may take over: a CSV-imported, active, undeleted
    Student of the class's own tenant. Nothing else, whatever ``_approve_one`` would allow."""
    return (
        u.tenant_id == tenant_id
        and u.deleted_at is None
        and u.is_active
        and u.role == UserRole.student
        and is_pre_provisioned(u)
    )


def matching_entries(db: Session, email: str, today: date | None = None) -> list[tuple[RollCallEntry, Class]]:
    """Entries ``email`` may claim (rule 3)."""
    today = today or datetime.now(UTC).date()
    rows = (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            RollCallEntry.email == email,
            RollCallEntry.status == RollCallStatus.invited.value,
            Class.status == ClassStatus.open.value,
            Class.end_date >= today,
            Class.tenant_id == RollCallEntry.tenant_id,
        )
        .order_by(Class.start_date, Class.id)
        .all()
    )
    out = []
    for entry, cls in rows:
        if entry.user_id is not None:
            # tenant-safe: the entry's own link, checked against the class's tenant below.
            linked = db.get(User, entry.user_id)
            if linked is None or not _adoptable_roster_row(linked, cls.tenant_id):
                continue
        out.append((entry, cls))
    return out


def _lead_as_approver(db: Session, cls: Class) -> CurrentUser | None:
    # tenant-safe: the class's lead, checked against the class's tenant on the next line.
    lead = db.get(User, cls.lead_instructor_id)
    if (
        lead is None
        or lead.tenant_id != cls.tenant_id
        or not lead.is_active
        or lead.deleted_at is not None
        or lead.role not in LEAD_ROLES
    ):
        return None
    return CurrentUser(
        id=str(lead.id), email=lead.email, display_name=lead.display_name, role=lead.role,
        tenant_id=str(lead.tenant_id), keycloak_id=str(lead.keycloak_id),
    )


def _fresh_request(db: Session, identity: TokenPayload, cls: Class, email: str) -> RegistrationRequest:
    """A new request built from the token's directory claims only, for ``_approve_one``.

    Never the identity's own pending request: its rank, unit, callsign, nation,
    justification and requested qualification or path were typed by the requester, and
    the roll call approves none of them. That request is withdrawn; the Student fills in
    their profile in the onboarding wizard, and the class's course is their only enrolment.
    """
    for old in (
        db.query(RegistrationRequest)
        .filter(RegistrationRequest.keycloak_id == identity.sub, RegistrationRequest.status == RegistrationStatus.pending)
        .all()
    ):
        old.status = RegistrationStatus.withdrawn
        old.decided_at = datetime.now(UTC)
        old.decision_reason = f"superseded by roll-call admission ({cls.name})"[:500]
    db.flush()  # before the insert: one pending request per identity (partial unique index)
    prefill = ident.claims_to_prefill(identity.model_dump())
    req = RegistrationRequest(
        keycloak_id=identity.sub,
        email=email,
        display_name=prefill["display_name"] or email,
        first_name=prefill["first_name"],
        last_name=prefill["last_name"],
        ad_object_guid=prefill["ad_object_guid"],
        ad_distinguished_name=prefill["ad_distinguished_name"],
        ad_groups="[]",
        timezone="UTC",
        requested_cohort=cls.name[:100],
        suggested_role="student",
        suggested_tenant_id=cls.tenant_id,
        status=RegistrationStatus.pending,
    )
    db.add(req)
    db.flush()
    return req


def admitted_from_roll_call(db: Session, user_id) -> bool:
    """Whether ``user_id``'s account was made by a roll-call admission (``roll_call_admit``,
    written by ``admit_from_roll_call``): they never filled in Request access, so the
    first-run wizard does not ask them to confirm one (ADR 0014 §1, #319)."""
    return (
        db.query(AuditLog.id)
        .filter(AuditLog.user_id == user_id, AuditLog.action == "roll_call_admit")
        .first()
        is not None
    )


def admit_from_roll_call(db: Session, identity: TokenPayload, client_ip: str | None) -> User | None:
    """Admit ``identity`` from a roll call and commit, or return None (Request access)."""
    email = trusted_email(identity)
    if email is None:
        return None
    matches = matching_entries(db, email)
    if not matches:
        return None
    tenants = {cls.tenant_id for _, cls in matches}
    if len(tenants) != 1:
        logger.warning("Roll-call match for sub %s is on %d tenants' roll calls; not admitting", identity.sub, len(tenants))
        return None
    denial = ident.auth_zone_denial(
        db, identity.model_dump(), client_ip, zone_name=ident.registration_zone_name(), check_mfa=False
    )
    if denial:
        return None

    # REGISTRATION_ALLOWED_GROUPS is deliberately not applied here: the owner decided the
    # roll call is the approval (ADR 0014 §1). The auth-zone check above still applies.

    first = matches[0][1]
    approver = _lead_as_approver(db, first)
    if approver is None:
        logger.warning("Class %s has no active lead in its tenant; roll-call admission skipped", first.id)
        return None

    # The account this email already names, if any, ignoring case and across tenants. Only
    # one, and only an adoptable roster Student of this tenant; anything else (a staff or
    # observer row, a deactivated or deleted one, another tenant's, two case variants) is
    # for an admin to resolve through Request access, never taken over here.
    existing = accounts_with_email(db, email)
    if len(existing) > 1 or (existing and not _adoptable_roster_row(existing[0], first.tenant_id)):
        logger.info("Roll-call admission for sub %s refused: the email names an existing account", identity.sub)
        return None
    adopt_as = existing[0].email if existing else email

    from ..routers.registration import _approve_one

    try:
        with db.begin_nested():
            req = _fresh_request(db, identity, first, adopt_as)
            user = _approve_one(
                db, req, approver, role="student", tenant_id=first.tenant_id,
                qualification_id=None, learning_path_id=None, note=f"roll call: {first.name}"[:500],
            )
            if user.role != UserRole.student or user.tenant_id != first.tenant_id:
                raise HTTPException(409, "roll-call admission produced something other than a Student")
            now = datetime.now(UTC)
            for entry, cls in matches:
                claimed = (
                    db.query(RollCallEntry)
                    .filter(RollCallEntry.id == entry.id, RollCallEntry.status == RollCallStatus.invited.value)
                    .update(
                        {
                            RollCallEntry.status: RollCallStatus.signed_in.value,
                            RollCallEntry.user_id: user.id,
                            RollCallEntry.signed_in_at: now,
                        },
                        synchronize_session=False,
                    )
                )
                if claimed != 1:
                    continue
                ensure_enrollment(db, user_id=user.id, course_id=cls.course_id, tenant_id=cls.tenant_id)
                db.add(
                    AuditLog(
                        user_id=user.id, tenant_id=cls.tenant_id, action="roll_call_admit",
                        resource_type="class", resource_id=str(cls.id),
                        detail=f"roll-call entry {entry.id} admitted on first sign-in (registration {req.id})",
                    )
                )
            db.flush()
        db.commit()
    except HTTPException as exc:
        # The savepoint is already rolled back: nothing of the attempt is left.
        logger.info("Roll-call admission for sub %s refused (%s); Request access applies", identity.sub, exc.detail)
        return None
    except IntegrityError:
        # A concurrent sign-in of the same identity created the account first.
        db.rollback()
        return db.query(User).filter(User.keycloak_id == identity.sub).first()
    db.refresh(user)
    logger.info("Roll call admitted sub %s as user %s into tenant %s", identity.sub, user.id, user.tenant_id)
    return user

