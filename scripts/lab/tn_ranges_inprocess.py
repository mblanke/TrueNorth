"""TrueNorth's ranges for scripts/lab/vcenter-organize.sh, read from inside the api image.

Prints one JSON list on stdout, nothing else: every range that is not destroyed, as
``{"id", "name", "state", "vms": [{"name", "template"}]}``. Only those fields are taken from
``provisioner_output``; addresses, accounts and anything else in it are not printed. Read
only: no row is changed.

    docker compose -f <app>/infra/platform/docker/compose.prod.yml --env-file <.env.production> \\
      run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v <kit checkout>:/srcapp:ro \\
      --entrypoint python api /srcapp/scripts/lab/tn_ranges_inprocess.py
"""

from __future__ import annotations

import json
import sys


def summarise(rows) -> list[dict]:
    """(id, name, state, provisioner_output) rows -> what the organize script needs."""
    out = []
    for rid, name, state, raw in rows:
        state = str(getattr(state, "value", state))
        if state == "destroyed":
            continue
        try:
            output = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            output = {}
        vms = [{"name": str(v.get("name")), "template": str(v.get("template") or "")}
               for v in (output.get("vms") or []) if isinstance(output, dict) and isinstance(v, dict) and v.get("name")]
        out.append({"id": str(rid), "name": str(name or ""), "state": state, "vms": vms})
    return out


def main() -> int:
    from app.db import SessionLocal
    from app.models import Range

    db = SessionLocal()
    try:
        rows = db.query(Range.id, Range.name, Range.state, Range.provisioner_output).all()
    finally:
        db.close()
    json.dump(summarise(rows), sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
