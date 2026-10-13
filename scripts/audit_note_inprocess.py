"""Write one audit_logs row for a host-side action, from inside the api image.

Used by playbooks/breakglass-reset-mfa.yml (roles/tn_keycloak/tasks/breakglass.yml) to
record an MFA reset in TrueNorth's own audit log, next to Keycloak's admin event:

    docker compose run --rm --no-deps -T -w /app -e PYTHONPATH=/app \\
      -v <app checkout>:/srcapp:ro --entrypoint python api \\
      /srcapp/scripts/audit_note_inprocess.py --action mfa_reset \\
      --subject <upn> --reason "<why>" --operator "<SUDO_USER>"

The subject's TrueNorth account (by email) supplies the tenant and resource id when it
exists; an identity with no account is still recorded, by name. The row's user_id is
empty: the actor is a host operator, named in ``detail``. Prints the row id.
"""

from __future__ import annotations

import argparse
import json
import sys

ACTIONS = ("mfa_reset",)


def note(db, *, action: str, subject: str, reason: str, operator: str):
    from app.models import AuditLog, User

    user = db.query(User).filter(User.email == subject).first()
    row = AuditLog(
        user_id=None,
        tenant_id=user.tenant_id if user else None,
        action=action,
        resource_type="user",
        resource_id=str(user.id) if user else subject,
        detail=json.dumps({"subject": subject, "reason": reason, "operator": operator,
                           "via": "playbooks/breakglass-reset-mfa.yml"}, sort_keys=True),
    )
    db.add(row)
    db.commit()
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--action", required=True, choices=ACTIONS)
    ap.add_argument("--subject", required=True, help="the user acted on (UPN / email)")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--operator", required=True, help="who ran it (the host login, SUDO_USER)")
    args = ap.parse_args(argv)
    if not args.reason.strip() or not args.operator.strip():
        print("--reason and --operator must not be empty", file=sys.stderr)
        return 2

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        row = note(db, action=args.action, subject=args.subject.strip(), reason=args.reason.strip(),
                   operator=args.operator.strip())
        print(f"audit row {row.id}: {args.action} {args.subject}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
