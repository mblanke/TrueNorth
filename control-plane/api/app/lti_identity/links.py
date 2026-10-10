"""Staff deep linking: an LMS account bound to a staff account by explicit confirmation.

The v1.0.0 known limitation: the LMS asserts the email claim and TrueNorth does not verify
it, so a launch whose email is a staff account's is refused (it must not *become* that
account), and staff whose LMS email is their TrueNorth email could not deep-link. Now:

1. A deep-linking launch that would have been refused that way stores a link request:
   the platform, the LMS subject, a hash of the asserted email, single use, ten minutes,
   always bound to an HttpOnly cookie on the launching browser (with
   ``LTI_REQUIRE_STATE_COOKIE=false`` no request is made at all: an unbound code could be
   phished). The LMS shows a page that opens TrueNorth at ``/lti/link#code=…``.
2. The staff member, signed in to TrueNorth with their own (Keycloak) sign-in in that
   browser, sees which LMS account and site it is and confirms. TrueNorth checks: the code
   is live and theirs (same browser), the platform is their tenant's and active, they are
   staff, the email the LMS asserted is their own, and neither side is linked already.
3. ``lti_user_links`` (platform, sub) -> user. From then on that LMS account's launches are
   that staff member, deep linking included. Only the account holder (or an integration
   admin of the tenant) removes it.

Nothing binds by email alone: an LMS that asserts a staff email gets a request nobody but
that staff member, signed in, in the same browser, can confirm. Students are unchanged.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ExternalPlatform, User, UserRole
from .models import LTILinkRequest, LTIUserLink

REQUEST_SECONDS = 600
LINK_COOKIE = "tn_lti_link"


class LinkError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def email_hash(email: str) -> str:
    return _hash((email or "").strip().lower())


def linked_user(db: Session, platform: ExternalPlatform, sub: str) -> User | None:
    """The TrueNorth account this LMS account was linked to, if any (same tenant, active)."""
    link = db.query(LTIUserLink).filter(LTIUserLink.platform_id == platform.id, LTIUserLink.lti_sub == sub).first()
    if link is None:
        return None
    user = db.get(User, link.user_id)  # tenant-safe: compared with the platform's tenant below
    if user is None or user.tenant_id != platform.tenant_id or not user.is_active or user.deleted_at is not None:
        return None
    return user


def request_link(db: Session, platform: ExternalPlatform, claims: dict, *, bind: str) -> str:
    """Store a link request for this launch; returns its code. Always browser-bound: an
    unbound code could be sent to a staff member to confirm (a phished privilege grant)."""
    if not bind:
        raise ValueError("a staff link request must be bound to the launching browser")
    code = secrets.token_urlsafe(32)
    name = str(claims.get("name") or " ".join(filter(None, (claims.get("given_name"), claims.get("family_name")))))
    db.add(
        LTILinkRequest(
            code_hash=_hash(code),
            bind_hash=_hash(bind),
            platform_id=platform.id,
            lti_sub=str(claims.get("sub", ""))[:255],
            email_hash=email_hash(str(claims.get("email") or "")),
            lms_name=name[:255],
            expires_at=datetime.now(UTC) + timedelta(seconds=REQUEST_SECONDS),
        )
    )
    db.commit()
    return code


def _live_request(db: Session, code: str, bind: str) -> LTILinkRequest:
    # tenant-safe: the code is the credential; the platform's tenant is checked by callers.
    row = db.query(LTILinkRequest).filter(LTILinkRequest.code_hash == _hash(code or "")).first()
    now = datetime.now(UTC)
    if row is None or row.used_at is not None:
        raise LinkError(404, "This link request is unknown or already used; deep-link again from your course")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= now:
        raise LinkError(410, "This link request has expired; deep-link again from your course")
    if not row.bind_hash or not bind or not secrets.compare_digest(row.bind_hash, _hash(bind)):
        raise LinkError(403, "Confirm the link in the browser you launched from")
    return row


def _check(db: Session, row: LTILinkRequest, user: User) -> ExternalPlatform:
    platform = db.get(ExternalPlatform, row.platform_id)  # tenant-safe: compared with the user's tenant
    if platform is None or platform.tenant_id != user.tenant_id or not platform.is_active:
        raise LinkError(404, "This link request is unknown or already used; deep-link again from your course")
    if (user.keycloak_id or "").startswith("lti:"):
        raise LinkError(403, "Sign in to TrueNorth with your own account to link it")
    if user.role == UserRole.student:
        raise LinkError(403, "Only staff link a learning-platform account; Students are matched automatically")
    if not secrets.compare_digest(row.email_hash, email_hash(user.email)):
        raise LinkError(403, "The learning platform named a different email than your TrueNorth account's")
    return platform


def preview(db: Session, code: str, bind: str, user: User) -> dict:
    """What the staff member is about to confirm (nothing is changed)."""
    row = _live_request(db, code, bind)
    platform = _check(db, row, user)
    return {"platform_id": platform.id, "platform_name": platform.name, "lms_name": row.lms_name}


def confirm(db: Session, code: str, bind: str, user: User) -> LTIUserLink:
    """Spend the request (once) and bind the LMS account to ``user``."""
    row = _live_request(db, code, bind)
    platform = _check(db, row, user)
    spent = db.execute(
        update(LTILinkRequest)
        .where(LTILinkRequest.id == row.id, LTILinkRequest.used_at.is_(None))
        .values(used_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    ).rowcount
    if spent != 1:
        db.rollback()
        raise LinkError(404, "This link request is unknown or already used; deep-link again from your course")
    taken = (
        db.query(LTIUserLink)
        .filter(
            LTIUserLink.platform_id == platform.id,
            (LTIUserLink.lti_sub == row.lti_sub) | (LTIUserLink.user_id == user.id),
        )
        .first()
    )
    if taken is not None:
        db.commit()  # the request is spent either way
        if taken.user_id == user.id and taken.lti_sub == row.lti_sub:
            return taken
        raise LinkError(409, "This learning-platform account, or your account on it, is already linked; unlink it first")
    link = LTIUserLink(platform_id=platform.id, lti_sub=row.lti_sub, user_id=user.id, lms_name=row.lms_name)
    db.add(link)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise LinkError(409, "This learning-platform account is already linked") from exc
    db.refresh(link)
    return link


def unlink(db: Session, link_id: uuid.UUID, user: User, *, admin: bool) -> None:
    link = db.get(LTIUserLink, link_id)  # tenant-safe: owner or same-tenant admin checked below
    owner = db.get(User, link.user_id) if link else None  # tenant-safe: the link's own user
    if link is None or owner is None or owner.tenant_id != user.tenant_id or (link.user_id != user.id and not admin):
        raise LinkError(404, "Link not found")
    db.delete(link)
    db.commit()
