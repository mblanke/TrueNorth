"""Claim interpretation for AD-federated identities.

Kept out of ``auth.py`` on purpose. ``auth.py`` stays a thin resolver; the
mapping rules below are pure and unit-testable without FastAPI, and the *same*
rules have to serve three callers: the registration form's prefill, the
approval screen's suggestions, and AD group sync.

The central rule: **AD group membership suggests, it never grants.** A group
can make someone eligible to *register* and can pre-fill what an approver sees,
but only an explicit approval assigns a role and a tenant. Everything here is
advisory.

Configuration is environment-driven so a deployment can re-map groups without
a code change:

``REGISTRATION_GROUP_ROLE_MAP``   JSON object, AD group -> UserRole value
``REGISTRATION_ALLOWED_GROUPS``   comma-separated; empty means "any AD user"
``REGISTRATION_DEFAULT_TENANT_SLUG``
``REGISTRATION_GROUP_TENANT_MAP`` JSON object, AD group -> tenant slug
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os

from sqlalchemy.orm import Session

from .models import AuthZonePolicy, Nation, Tenant, UserRole

logger = logging.getLogger(__name__)

# Defaults match the AD groups created on TN-DC01; see the deployment repo's
# docs/deployment/identity-plan.md. "trainee" is a legacy alias kept so a token
# minted before the student/trainee vocabulary was reconciled still resolves.
DEFAULT_GROUP_ROLE_MAP: dict[str, str] = {
    "TN-Platform-Admins": UserRole.admin.value,
    "TN-Range-Operators": UserRole.range_ops.value,
    "TN-Instructors": UserRole.instructor.value,
    "TN-Content-Authors": UserRole.instructor.value,
    "TN-Students": UserRole.student.value,
    "TN-Observers": UserRole.observer.value,
    "trainee": UserRole.student.value,
}

# Highest wins when someone is in several groups. Explicit rather than relying
# on dict or set ordering, so multi-group membership is deterministic.
ROLE_PRECEDENCE: tuple[UserRole, ...] = (
    UserRole.admin,
    UserRole.range_ops,
    UserRole.instructor,
    UserRole.observer,
    UserRole.student,
)


def _json_env(name: str, default: dict[str, str]) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("%s is not valid JSON; falling back to defaults", name)
        return dict(default)
    if not isinstance(parsed, dict):
        logger.warning("%s must be a JSON object; falling back to defaults", name)
        return dict(default)
    return {str(k): str(v) for k, v in parsed.items()}


def group_role_map() -> dict[str, str]:
    return _json_env("REGISTRATION_GROUP_ROLE_MAP", DEFAULT_GROUP_ROLE_MAP)


def group_tenant_map() -> dict[str, str]:
    return _json_env("REGISTRATION_GROUP_TENANT_MAP", {})


def allowed_groups() -> set[str]:
    """Groups permitted to register. Empty set means no group restriction."""
    raw = os.getenv("REGISTRATION_ALLOWED_GROUPS", "").strip()
    return {g.strip() for g in raw.split(",") if g.strip()} if raw else set()


def normalise_groups(claim: object) -> list[str]:
    """Coerce a ``groups`` claim into a clean list of names.

    Keycloak's group-membership mapper emits paths like ``/TN-Students`` when
    ``full.path`` is true; strip to the leaf so configuration can be written in
    terms of plain group names either way.
    """
    if claim is None:
        return []
    if isinstance(claim, str):
        items: list[object] = [p for p in claim.replace(",", " ").split() if p]
    elif isinstance(claim, (list, tuple, set)):
        items = list(claim)
    else:
        return []

    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        if "/" in name:
            name = name.rstrip("/").rsplit("/", 1)[-1]
        if name and name not in out:
            out.append(name)
    return out


def may_register(groups: list[str]) -> bool:
    """Whether an AD identity in ``groups`` is eligible to submit a request."""
    allowed = allowed_groups()
    if not allowed:
        return True
    return bool(allowed.intersection(groups))


def suggest_role(groups: list[str]) -> UserRole | None:
    """Highest-precedence role implied by AD group membership. Advisory only."""
    mapping = group_role_map()
    matched: set[UserRole] = set()
    for g in groups:
        value = mapping.get(g)
        if value is None:
            continue
        try:
            matched.add(UserRole(value))
        except ValueError:
            logger.warning("group %r maps to unknown role %r", g, value)
    for role in ROLE_PRECEDENCE:
        if role in matched:
            return role
    return None


def suggest_tenant(db: Session, groups: list[str]) -> Tenant | None:
    """Tenant implied by group membership, else the configured default."""
    mapping = group_tenant_map()
    for g in groups:
        slug = mapping.get(g)
        if slug:
            tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
            if tenant is not None:
                return tenant
            logger.warning("group %r maps to unknown tenant slug %r", g, slug)

    default_slug = os.getenv("REGISTRATION_DEFAULT_TENANT_SLUG", "default").strip() or "default"
    return db.query(Tenant).filter(Tenant.slug == default_slug).first()


def claims_to_prefill(claims: dict) -> dict:
    """The parts of a registration form AD can already answer.

    Everything here is read-only in the UI: it comes from the directory, so a
    trainee correcting it would only desynchronise the two.
    """
    given = (claims.get("given_name") or "").strip()
    family = (claims.get("family_name") or "").strip()
    display = (claims.get("name") or "").strip() or " ".join(p for p in (given, family) if p)
    if not display:
        display = (claims.get("preferred_username") or "").strip()

    return {
        "email": (claims.get("email") or "").strip(),
        "display_name": display,
        "first_name": given or None,
        "last_name": family or None,
        "ad_object_guid": (claims.get("ad_object_guid") or claims.get("ldap_id") or None),
        "ad_distinguished_name": (claims.get("ad_distinguished_name") or None),
    }


def resolve_nation(db: Session, code: str | None) -> Nation | None:
    """Look a nation up by ISO alpha-2 or alpha-3 code."""
    if not code:
        return None
    key = code.strip().upper()
    if not key:
        return None
    return (
        db.query(Nation)
        .filter((Nation.iso_alpha2 == key) | (Nation.iso_alpha3 == key))
        .first()
    )


# ── Auth zone enforcement ──────────────────────────────────────────────
CLEARANCE_ORDER: tuple[str, ...] = (
    "unclassified",
    "protected_a",
    "protected_b",
    "protected_c",
    "confidential",
    "secret",
    "top_secret",
)


def _clearance_rank(value: str | None) -> int:
    if not value:
        return 0
    try:
        return CLEARANCE_ORDER.index(value.strip().lower())
    except ValueError:
        return 0


def _ip_permitted(whitelist: str | None, client_ip: str | None) -> bool:
    """Whether ``client_ip`` falls inside a comma-separated CIDR/address list.

    An empty whitelist means unrestricted. An unparseable client address fails
    closed only when a whitelist is actually configured.
    """
    entries = [e.strip() for e in (whitelist or "").split(",") if e.strip()]
    if not entries:
        return True
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("auth zone whitelist entry %r is not a valid network", entry)
    return False


def auth_zone_denial(
    db: Session,
    claims: dict,
    client_ip: str | None,
    *,
    zone_name: str | None = None,
) -> str | None:
    """Return a human-readable denial reason, or ``None`` if the zone permits.

    Returns a reason rather than raising so both ``get_current_user`` and the
    registration endpoint can apply the same policy while shaping their own
    error responses.
    """
    q = db.query(AuthZonePolicy).filter(AuthZonePolicy.is_active.is_(True))
    policy = (
        q.filter(AuthZonePolicy.zone_name == zone_name).first()
        if zone_name
        else q.order_by(AuthZonePolicy.zone_name).first()
    )
    if policy is None:
        return None

    if policy.require_mfa:
        amr = claims.get("amr") or []
        if isinstance(amr, str):
            amr = [amr]
        acr = str(claims.get("acr") or "")
        mfa_present = bool({"mfa", "otp", "hwk", "fido", "swk"}.intersection(str(a).lower() for a in amr))
        # acr "0" means "no authentication context" — explicitly not MFA.
        if not mfa_present and acr in ("", "0"):
            return f"Auth zone '{policy.zone_name}' requires multi-factor authentication"

    if _clearance_rank(claims.get("clearance_level")) < _clearance_rank(policy.clearance_required):
        return (
            f"Auth zone '{policy.zone_name}' requires clearance "
            f"'{policy.clearance_required}'"
        )

    if not _ip_permitted(policy.ip_whitelist, client_ip):
        return f"Auth zone '{policy.zone_name}' does not permit access from {client_ip or 'unknown'}"

    return None
