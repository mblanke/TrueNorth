"""Run the daily signed audit export once, now (ADR 0011): ``python -m worker.audit_export_now``.

The installer runs this in worker-scenario at the end of every install or upgrade
(install/playbooks/99-validate.yml), so a host is exported through yesterday at once instead
of at beat's next 00:45 UTC, and deep health's ``audit_export`` is current straight away.

It is the beat task's own body (``audit_tasks.export_audit_days``), run in-process: idempotent
(it exports only days with no export yet, so a second run writes nothing) and it never writes
anything unsigned. Prints one JSON line. Exit 0 on success; 1 without a usable
AUDIT_SIGNING_SEED; 2 when the export fails (the error class and text go to stderr, never
the seed).
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from . import audit_signing
    from .audit_tasks import export_audit_days

    try:
        result = export_audit_days.run()
    except audit_signing.SigningKeyError as exc:
        print(f"audit export: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # the archive refuses writes, the database is down, ...
        print(f"audit export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"exported": result["exported"], "key_id": result["key_id"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
