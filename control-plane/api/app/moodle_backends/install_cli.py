"""The installer's hooks for a Moodle it runs itself (install/roles/tn_moodle).

Run inside the api container, which already holds the database and the tool key, so the
installer needs no admin token and no Keycloak account to wire Moodle up:

    python -m app.moodle_backends.install_cli tenant <tenant slug>
        prints the tenant's id (the Moodle's TN_TENANT_ID: it accepts tickets for no other)

    python -m app.moodle_backends.install_cli register <tenant slug> <node> <base url> < registration.json
        creates or updates platform ``moodle-<node>`` in that tenant from the JSON the Moodle
        wrote at start-up (moodledata/truenorth-registration.json); prints one JSON line
        {"action": "created" | "updated" | "unchanged", "platform_id": ...}

    python -m app.moodle_backends.install_cli manage <tenant slug or id> <node>
        marks platform ``moodle-<node>`` (one scripts/moodle-farm.sh registered through the
        API) as a TrueNorth farm node; ``register`` marks its node itself
        (app/moodle_farm/service.py: what being a farm node allows)

    python -m app.moodle_backends.install_cli check <tenant slug> <node>
        asks that Moodle, through the publishing backend and a signed sync ticket, to
        describe a course that never exists: proves the address, the Host routing, the
        plugin, Moodle's trust in this TrueNorth's key and its tenant, end to end

Exit codes: 0 ok; 2 bad input; 3 no such tenant or platform; 4 the issuer belongs to
another tenant; 5 Moodle refused or could not be reached.

scripts/moodle-farm.sh registers farm nodes through the API instead; both write the same
platform shape (slug ``moodle-<node>``, ``base_url`` the internal address, LTI URLs public).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy.orm import Session

# A course idnumber local_truenorth accepts (a stage of the nil UUID) and no release has.
PROBE_IDNUMBER = "tn-stage:00000000-0000-0000-0000-000000000000"
REGISTRATION_KEYS = (
    "lti_issuer",
    "lti_client_id",
    "lti_deployment_id",
    "lti_auth_login_url",
    "lti_token_url",
    "lti_jwks_url",
)


class InstallError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def platform_slug(node: str) -> str:
    return f"moodle-{node}"


def tenant_id(db: Session, slug: str) -> Any:
    from ..models import Tenant

    tenant = db.query(Tenant).filter(Tenant.slug == slug).one_or_none()
    if tenant is None:
        raise InstallError(f"no tenant with slug {slug!r} (80-seed creates it)", 3)
    return tenant.id


def register(db: Session, tenant_slug: str, node: str, base_url: str, registration: dict[str, Any]) -> dict[str, str]:
    """Create or converge platform ``moodle-<node>`` in the tenant. Commits."""
    from ..models import ExternalPlatform, IntegrationAuthType

    missing = [k for k in REGISTRATION_KEYS if not str(registration.get(k) or "").strip()]
    if missing:
        raise InstallError(f"the Moodle's registration lacks {', '.join(missing)}", 2)
    if not base_url.startswith(("http://", "https://")):
        raise InstallError(f"base url must be http(s): {base_url!r}", 2)
    tid = tenant_id(db, tenant_slug)
    issuer = str(registration["lti_issuer"]).rstrip("/")
    # One issuer belongs to one tenant: it is the audience of the tickets this API signs
    # with the key every tenant's Moodle trusts (routers/integrations.py _check_issuer).
    # tenant-safe: a cross-tenant existence check by design; it returns no row's data.
    taken = (
        db.query(ExternalPlatform.id)
        .filter(ExternalPlatform.lti_issuer.in_([issuer, issuer + "/"]), ExternalPlatform.tenant_id != tid)
        .first()
    )
    if taken:
        raise InstallError(f"{issuer} is registered to another tenant", 4)

    wanted: dict[str, Any] = {
        "name": f"Moodle ({node})",
        "platform_type": "moodle",
        "base_url": base_url.rstrip("/"),
        "auth_type": IntegrationAuthType.lti13,
        "is_active": True,
        **{k: str(registration[k]).strip() for k in REGISTRATION_KEYS},
    }
    wanted["lti_issuer"] = issuer
    slug = platform_slug(node)
    platform = (
        db.query(ExternalPlatform)
        .filter(ExternalPlatform.tenant_id == tid, ExternalPlatform.slug == slug)
        .one_or_none()
    )
    if platform is None:
        platform = ExternalPlatform(tenant_id=tid, slug=slug, **wanted)
        db.add(platform)
        action = "created"
    else:
        changed = {k: v for k, v in wanted.items() if getattr(platform, k) != v}
        for k, v in changed.items():
            setattr(platform, k, v)
        action = "updated" if changed else "unchanged"
    db.flush()
    # The installer runs this Moodle: it is a farm node (app/moodle_farm/service.py).
    from ..moodle_farm import service as farm

    if farm.mark(db, platform, node, "install_cli register") and action == "unchanged":
        action = "updated"
    db.commit()
    db.refresh(platform)
    return {"action": action, "platform_id": str(platform.id), "tenant_id": str(tid), "slug": slug}


def manage(db: Session, tenant: str, node: str) -> dict[str, str]:
    """Mark platform ``moodle-<node>`` (registered through the API by scripts/moodle-farm.sh)
    a farm node. ``tenant``: its slug or id. Run only by whoever runs the api container."""
    import uuid as _uuid

    from ..models import ExternalPlatform, Tenant
    from ..moodle_farm import service as farm

    try:
        tid = _uuid.UUID(tenant)
        if db.get(Tenant, tid) is None:
            raise InstallError(f"no tenant {tenant}", 3)
    except ValueError:
        tid = tenant_id(db, tenant)
    platform = (
        db.query(ExternalPlatform)
        .filter(ExternalPlatform.tenant_id == tid, ExternalPlatform.slug == platform_slug(node))
        .one_or_none()
    )
    if platform is None:
        raise InstallError(f"no platform {platform_slug(node)} in tenant {tenant!r}", 3)
    if platform.platform_type != "moodle":
        raise InstallError(f"{platform_slug(node)} is not a Moodle", 2)
    action = "marked" if farm.mark(db, platform, node, "install_cli manage") else "unchanged"
    db.commit()
    return {"action": action, "platform_id": str(platform.id)}


def check(db: Session, tenant_slug: str, node: str, backend: Any = None) -> dict[str, Any]:
    """Describe a course that never exists through the publishing backend."""
    from .. import lti13
    from ..models import ExternalPlatform
    from . import MoodleError, get_moodle_backend

    tid = tenant_id(db, tenant_slug)
    platform = (
        db.query(ExternalPlatform)
        .filter(ExternalPlatform.tenant_id == tid, ExternalPlatform.slug == platform_slug(node))
        .one_or_none()
    )
    if platform is None:
        raise InstallError(f"no platform {platform_slug(node)} in tenant {tenant_slug!r}", 3)

    def key() -> tuple[str, str]:
        k = lti13.get_tool_key(db)
        return lti13.signing_pem(k), k.kid

    backend = backend or get_moodle_backend(platform.platform_type, key_provider=key)
    try:
        described = backend.describe_course(platform, PROBE_IDNUMBER)
    except MoodleError as exc:
        raise InstallError(str(exc), 5) from exc
    if described.get("exists") is not False:
        raise InstallError(f"Moodle answered describe_course unexpectedly: {described}", 5)
    return {"ok": True, "platform_id": str(platform.id), "issuer": platform.lti_issuer}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.moodle_backends.install_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tenant")
    p.add_argument("tenant_slug")
    p = sub.add_parser("register")
    p.add_argument("tenant_slug")
    p.add_argument("node")
    p.add_argument("base_url")
    p = sub.add_parser("check")
    p.add_argument("tenant_slug")
    p.add_argument("node")
    p = sub.add_parser("manage")
    p.add_argument("tenant")
    p.add_argument("node")
    args = parser.parse_args(argv)

    from ..db import SessionLocal

    db = SessionLocal()
    try:
        if args.cmd == "tenant":
            print(tenant_id(db, args.tenant_slug))
        elif args.cmd == "register":
            try:
                registration = json.loads(sys.stdin.read())
            except ValueError as exc:
                raise InstallError(f"registration on stdin is not JSON: {exc}", 2) from exc
            if not isinstance(registration, dict):
                raise InstallError("registration on stdin is not a JSON object", 2)
            print(json.dumps(register(db, args.tenant_slug, args.node, args.base_url, registration)))
        elif args.cmd == "manage":
            print(json.dumps(manage(db, args.tenant, args.node)))
        else:
            print(json.dumps(check(db, args.tenant_slug, args.node)))
    except InstallError as exc:
        db.rollback()
        print(f"install_cli {args.cmd}: {exc}", file=sys.stderr)
        return exc.code
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
