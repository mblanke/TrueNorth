"""TrueNorth's own Moodle farm nodes: what being one allows, and binding their accounts.

A farm node (``ManagedMoodleNode``) is a Moodle the installer started and registered from
inside the api container (``install_cli register``, or ``install_cli manage`` for a node
``scripts/moodle-farm.sh`` added). Two things follow, for those platforms only:

1. **Farm accounts are bound to their TrueNorth Student** (``bind_farm_account``). Students
   reach a farm Moodle through TrueNorth's sign-in (local_truenorth ``sso.php``), which
   creates their Moodle account as username ``tn-<TrueNorth user id>`` with idnumber the
   TrueNorth user id, and locks the idnumber field (``auth_manual/field_lock_idnumber``) so a
   Student cannot edit it (#132). Moodle 5.2.3 sends both in every LTI 1.3 launch (checked
   against the real Moodle, tests/integration/test_moodle_cmi5_lti.py): the idnumber as
   ``lis.person_sourcedid`` and the username as ``ext.user_username``; ``sub`` is Moodle's
   own user id. A launch whose sourcedid is a Student of the platform's tenant and whose
   username is exactly ``tn-<that id>`` is bound: an ``LTIUserLink`` (platform, sub) ->
   Student is recorded, so grades go back to it (``lti_identity.links.is_bound``). Both
   must agree: a self-chosen username alone (self-registration) has no idnumber, and an
   idnumber alone is something any Moodle admin can type on any account. Never from email,
   never on a platform that is not a farm node, never across tenants, never for staff
   (they link explicitly, signed in: lti_identity.links).
2. **Its server-side traffic may reach a private address** (the node's internal
   ``base_url``, e.g. ``http://moodle-default:8080``) without turning on
   ``INTEGRATION_ALLOW_PRIVATE_URLS`` for every platform (``lti13._allow_private``).

Changing a farm node's addresses or LTI identity through the API ends its farm status
(``routers/integrations.py`` update_platform); the installer's next run marks it again.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from ..lti_identity.models import LTIUserLink
from ..models import ExternalPlatform, User, UserRole
from .models import ManagedMoodleNode

logger = logging.getLogger("truenorth.moodle_farm")

CLAIM_LIS = "https://purl.imsglobal.org/spec/lti/claim/lis"
CLAIM_EXT = "https://purl.imsglobal.org/spec/lti/claim/ext"
# The registration fields that make a platform the node the installer registered.
IDENTITY_FIELDS = (
    "base_url", "lti_issuer", "lti_client_id", "lti_deployment_id", "lti_auth_login_url", "lti_token_url",
    "lti_jwks_url",
)


def is_managed(db: Session, platform: ExternalPlatform | None) -> bool:
    if platform is None or platform.id is None:
        return False
    row = db.get(ManagedMoodleNode, platform.id)  # tenant-safe: compared with the platform's tenant
    return row is not None and row.tenant_id == platform.tenant_id


def mark(db: Session, platform: ExternalPlatform, node: str, by: str) -> bool:
    """Mark ``platform`` a farm node. True if it was not one. The caller commits."""
    row = db.get(ManagedMoodleNode, platform.id)  # tenant-safe: the installer's own platform
    if row is not None and row.tenant_id == platform.tenant_id:
        return False
    if row is None:
        db.add(ManagedMoodleNode(platform_id=platform.id, tenant_id=platform.tenant_id, node=node[:64], marked_by=by))
    else:
        row.tenant_id, row.node, row.marked_by = platform.tenant_id, node[:64], by
    db.flush()
    return True


def unmark(db: Session, platform_id: uuid.UUID) -> bool:
    # tenant-safe: the caller's own platform (get_owned) or the installer's.
    return bool(db.query(ManagedMoodleNode).filter(ManagedMoodleNode.platform_id == platform_id).delete())


def farm_student(db: Session, platform: ExternalPlatform, claims: dict) -> User | None:
    """The Student a farm node's launch is from, by the locked TrueNorth id and the username
    TrueNorth's sign-in gave the account; None unless both agree (and see the docstring)."""
    if not is_managed(db, platform):
        return None
    lis = claims.get(CLAIM_LIS) if isinstance(claims.get(CLAIM_LIS), dict) else {}
    ext = claims.get(CLAIM_EXT) if isinstance(claims.get(CLAIM_EXT), dict) else {}
    try:
        tn_id = uuid.UUID(str(lis.get("person_sourcedid") or ""))
    except ValueError:
        return None
    if str(ext.get("user_username") or "") != f"tn-{tn_id}":
        return None
    user = db.get(User, tn_id)  # tenant-safe: compared with the platform's tenant below
    if (
        user is None
        or user.tenant_id != platform.tenant_id
        or user.role != UserRole.student
        or not user.is_active
        or user.deleted_at is not None
    ):
        return None
    return user


def bind_farm_account(db: Session, platform: ExternalPlatform, claims: dict) -> User | None:
    """Bind this farm launch's LMS account to its Student (an ``LTIUserLink``) and return
    the Student, or None when it is not a farm Student's launch or cannot be bound."""
    sub = str(claims.get("sub") or "")
    student = farm_student(db, platform, claims) if sub else None
    if student is None:
        return None
    # tenant-safe: this platform's links of this sub or this Student.
    links = (
        db.query(LTIUserLink)
        .filter(LTIUserLink.platform_id == platform.id)
        .filter((LTIUserLink.lti_sub == sub) | (LTIUserLink.user_id == student.id))
        .all()
    )
    if any(link.lti_sub == sub and link.user_id == student.id for link in links):
        return student
    if links:
        # The sub is linked to someone else, or the Student to another Moodle account (a
        # re-created one): never re-pointed silently. Grades stay in TrueNorth until an
        # integration admin removes the old link.
        logger.warning("farm account on %s not bound: an existing link conflicts", platform.id)
        return None
    ext = claims.get(CLAIM_EXT) if isinstance(claims.get(CLAIM_EXT), dict) else {}
    db.add(LTIUserLink(platform_id=platform.id, lti_sub=sub[:255], user_id=student.id,
                       lms_name=str(ext.get("user_username") or "")[:255]))
    db.commit()
    logger.info("farm account on %s bound to Student %s", platform.id, student.id)
    return student
