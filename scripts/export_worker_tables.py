#!/usr/bin/env python3
"""Generate the worker's view of the API-owned tables it reads and writes.

The worker builds as its own image and cannot import ``app.models``; until 2026-10-03 it
wrote those tables with raw SQL, so a renamed table or column broke it silently in
production (it inserted into ``aars``, a table that never existed). This script renders
SQLAlchemy Core ``Table`` objects for exactly the tables in ``WORKER_TABLES`` from the
API's metadata, into ``control-plane/worker/worker/tables.py``. Worker code uses those,
so a schema change it depends on fails at import or in tests, not in production.

    python scripts/export_worker_tables.py          # regenerate
    python scripts/export_worker_tables.py --check  # fail on drift (dod.sh, CI)
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "control-plane/worker/worker/tables.py"

# Tables the worker touches. Adding one here is how the worker gains access to it.
WORKER_TABLES = (
    "after_action_reports",
    "audit_export_checks",
    "audit_exports",
    "audit_logs",
    "competencies",
    "competency_assertions",
    "competency_auto_assessments",
    "content_feed_entries",  # ADR 0014 §25: the feeds' pull stores them; the content clock's sync reads them
    "content_feed_flags",
    "content_feed_sources",
    "content_review_cycles",  # ADR 0014 §22-24: the content clock's sync (worker/content_review_sync.py)
    "content_review_edges",
    "content_review_items",
    "content_review_themes",
    "course_modules",  # read by the content clock's sync, to find what each course depends on
    "courses",
    "exercise_runs",
    "exercises",
    "forged_exercises",
    "golden_image_builds",
    "golden_images",
    "hypervisor_connections",
    "inject_records",
    "lab_sessions",
    "learning_recommendations",
    "maintenance_windows",
    "module_content",  # read by the content clock's sync
    "network_reservations",
    "objectives",
    "range_credential_snapshots",
    "range_certification_entries",  # ADR 0013 §6: worker-side slices append evidence (never update)
    "range_certification_records",
    "range_credentials",
    "range_greyspace",
    "range_inventory_checks",  # ADR 0013 §5: the worker writes teardown verification and drift
    "range_leases",
    "range_operations",
    "range_snapshots",
    "ranges",
    "scenario_executions",
    "scenarios",
    "scheduled_events",  # read by the content clock's sync: a booked class uses its range template
    "templates",
    "users",
)

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(ROOT / "control-plane/api"))


def _type_src(t) -> str:
    import sqlalchemy as sa

    name = type(t).__name__
    if name == "GUID":
        return "GUID()"
    if name == "SealedText":  # sealed by the API (app/sealed_column.py); the worker unseals it itself
        return "sa.Text()"
    if isinstance(t, sa.Enum):
        members = ", ".join(repr(e) for e in t.enums)
        return f"sa.Enum({members}, name={t.name!r})"
    if isinstance(t, sa.DateTime):
        return f"sa.DateTime(timezone={bool(t.timezone)})"
    if isinstance(t, sa.String) and not isinstance(t, sa.Text) and t.length:
        return f"sa.String({t.length})"
    if type(t) in (sa.Text, sa.String, sa.Integer, sa.Float, sa.Boolean, sa.JSON, sa.BigInteger, sa.Date):
        return f"sa.{name}()"
    raise SystemExit(f"export_worker_tables: no rendering for column type {type(t)!r}; extend _type_src")


def render() -> str:
    import app.audit_integrity  # noqa: F401 — audit_exports
    import app.content_feeds  # noqa: F401 — the feeds (worker/content_feeds)
    import app.content_review  # noqa: F401 — the content clock (worker/content_review_sync.py)
    import app.golden_image_builds  # noqa: F401 — golden_image_builds (worker/template_pins.py)
    import app.greyspace.models  # noqa: F401
    import app.lab_sessions  # noqa: F401 — lab_sessions
    import app.maintenance_windows  # noqa: F401 — the worker re-checks windows at a range task's entry
    import app.network_inventory  # noqa: F401 — section models register their tables on import
    import app.range_certification  # noqa: F401
    import app.range_credentials  # noqa: F401
    import app.range_inventory  # noqa: F401 — range_inventory_checks (worker/range_teardown.py, range_drift.py)
    import app.range_leases  # noqa: F401
    import app.range_ops  # noqa: F401 — range_operations.window_bypass (worker/maintenance_windows.py)
    import app.scenario_runs  # noqa: F401
    import app.scheduler.models  # noqa: F401 — scheduled_events
    from app import models
    from app.db import Base

    md = Base.metadata
    missing = [t for t in WORKER_TABLES if t not in md.tables]
    if missing:
        raise SystemExit(f"export_worker_tables: not API tables: {missing}")

    guid_src = inspect.getsource(models.GUID)
    out = [
        '"""GENERATED by scripts/export_worker_tables.py from control-plane/api/app/models.py. Do not edit.',
        "",
        "The API-owned tables the worker may read and write (Core, not ORM). Only column names",
        "and types are mirrored: Python-side defaults (ids, timestamps) are NOT, so supply them",
        "explicitly on insert. To use another table, add it to WORKER_TABLES in the script.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import uuid",
        "",
        "import sqlalchemy as sa",
        "from sqlalchemy import CHAR, TypeDecorator",
        "from sqlalchemy.dialects.postgresql import UUID as PG_UUID",
        "",
        "metadata = sa.MetaData()",
        "",
        "",
        guid_src.rstrip(),
        "",
    ]
    for tname in WORKER_TABLES:
        table = md.tables[tname]
        out += ["", f"{tname} = sa.Table(", f"    {tname!r},", "    metadata,"]
        for col in table.columns:
            flags = []
            if col.primary_key:
                flags.append("primary_key=True")
            if not col.nullable and not col.primary_key:
                flags.append("nullable=False")
            if col.unique:
                flags.append("unique=True")
            extra = (", " + ", ".join(flags)) if flags else ""
            out.append(f"    sa.Column({col.name!r}, {_type_src(col.type)}{extra}),")
        out.append(")")
    out += ["", f"__all__ = {sorted(['metadata', 'GUID', *WORKER_TABLES])!r}", ""]
    # Format with the repo's ruff so the committed file is exactly what this produces.
    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(OUT), "-"],
        input="\n".join(out),
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return formatted.stdout


def main() -> int:
    text = render()
    if "--check" not in sys.argv:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(ROOT)}")
        return 0
    if OUT.exists() and OUT.read_text() == text:
        print(f"  {OUT.relative_to(ROOT)} matches the API models")
        return 0
    print(
        "WORKER TABLE DRIFT: the API schema changed under the worker. Run "
        "`python scripts/export_worker_tables.py`, then fix any worker code that no longer fits.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
