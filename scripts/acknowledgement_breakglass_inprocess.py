"""Break-glass for the rules of use (ADR 0014 §3): sign for a stuck user, or republish the
shipped text over a bad version. Run inside the api image on the host, like
scripts/audit_note_inprocess.py (docs/runbooks/break-glass.md, "Rules of use"):

    docker compose run --rm --no-deps -T -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/acknowledgement_breakglass_inprocess.py sign \\
      --as <email> --reason "<why>" --operator "<SUDO_USER>"

    ... acknowledgement_breakglass_inprocess.py publish-default \\
      --as <platform admin email> --reason "<why>" --operator "<SUDO_USER>"

``sign`` records the named user's signature of the current version (so they can reach
the admin page and fix what is wrong). ``publish-default`` publishes the shipped default
text as the next version, superseding a version nobody can agree to (everyone signs the
new one). Both need root and Docker on the host, and each writes an
``acknowledgement_breakglass`` audit row naming the operator and the reason, besides the
usual ``acknowledgement_sign`` / ``acknowledgement_publish`` rows.
"""

from __future__ import annotations

import argparse
import json
import sys


def _account(db, email: str):
    from app.auth import CurrentUser
    from app.models import User

    row = db.query(User).filter(User.email == email.strip()).first()
    if row is None or not row.is_active:
        raise SystemExit(f"no active TrueNorth account with email {email!r}")
    return row, CurrentUser(id=str(row.id), email=row.email, display_name=row.display_name, role=row.role,
                            tenant_id=str(row.tenant_id), keycloak_id=str(row.keycloak_id))


def _note(db, row, *, what: str, reason: str, operator: str, version: int | None) -> None:
    from app.models import AuditLog

    db.add(AuditLog(user_id=row.id, tenant_id=None, action="acknowledgement_breakglass",
                    resource_type="user", resource_id=str(row.id),
                    detail=json.dumps({"what": what, "reason": reason, "operator": operator, "version": version,
                                       "via": "scripts/acknowledgement_breakglass_inprocess.py"}, sort_keys=True)))


def sign(db, *, email: str, reason: str, operator: str) -> str:
    from app.acknowledgement import rule, service

    row, user = _account(db, email)
    current = rule.current_version(db)
    if current is None:
        raise SystemExit("no version is published: nobody is asked to sign")
    service.sign(db, user, current.id, ip=None, user_agent=f"inprocess breakglass ({operator})")
    _note(db, row, what="sign", reason=reason, operator=operator, version=current.version)
    db.commit()
    return f"{email}: signed version {current.version}"


def publish_default(db, *, email: str, reason: str, operator: str) -> str:
    from app.acknowledgement import service
    from app.acknowledgement.default_text import DEFAULT_TEXT
    from app.rbac import is_platform_admin

    row, user = _account(db, email)
    if not is_platform_admin(user, db):
        raise SystemExit(f"{email} is not the platform administrator")
    draft = service.create_draft(db, row.id, DEFAULT_TEXT)
    published = service.publish(db, draft.id, row.id)
    _note(db, row, what="publish_default", reason=reason, operator=operator, version=published.version)
    db.commit()
    return f"published version {published.version} (the shipped default text) as {email}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("action", choices=("sign", "publish-default"))
    ap.add_argument("--as", dest="email", required=True, help="the TrueNorth account (email) to act as")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--operator", required=True, help="who ran it (the host login, SUDO_USER)")
    args = ap.parse_args(argv)
    if not args.reason.strip() or not args.operator.strip():
        print("--reason and --operator must not be empty", file=sys.stderr)
        return 2

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        act = sign if args.action == "sign" else publish_default
        print(act(db, email=args.email, reason=args.reason.strip(), operator=args.operator.strip()))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
