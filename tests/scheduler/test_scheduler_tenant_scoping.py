"""The tenant-scoping guard, applied to the scheduler section (ADR 0004).

tests/api/test_tenant_scoping_guard.py scans ``app/routers/*.py`` only; the scheduler
lives in its own section package (ADR 0003), so its router and services would never be
scanned. This runs the same two detectors over ``app/scheduler/`` with no baseline:
every by-id lookup of a tenant-owned model must carry a tenant predicate or a
justified ``# tenant-safe:`` waiver.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = ROOT / "control-plane/api/app/scheduler"


def _guard():
    spec = importlib.util.spec_from_file_location("_tenant_guard", ROOT / "tests/api/test_tenant_scoping_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(src: str, start: int) -> int:
    return src[:start].count("\n") + 1


def _waived(src: str, start: int) -> bool:
    """A justified ``# tenant-safe:`` comment in the 4 lines before, as the guard allows."""
    line = _line(src, start)
    return "tenant-safe:" in "\n".join(src.splitlines()[max(0, line - 5) : line])


def _findings() -> list[str]:
    import app.main  # noqa: F401  every model registered, the scheduler's included

    g = _guard()
    tenanted = g._tenanted_models()
    out: list[str] = []
    for f in sorted(SCHEDULER.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        where = f.relative_to(SCHEDULER)
        for m in g.QUERY.finditer(src):
            model, body = m.group(1), m.group(2)
            if model not in tenanted or not g.BY_ID.search(body) or "tenant_id" in body:
                continue
            if re.search(r"user\.id|user_id", body) or _waived(src, m.start()):
                continue
            out.append(f"{where}:{_line(src, m.start())} query({model}) by id")
        for m in g.DB_GET.finditer(src):
            if m.group(1) in tenanted and not _waived(src, m.start()):
                out.append(f"{where}:{_line(src, m.start())} db.get({m.group(1)})")
    return out


def test_the_scan_flags_an_unwaived_db_get():
    """A guard that cannot fail is not a guard."""
    g = _guard()
    src = "def f(db, x):\n    return db.get(ScheduledEvent, x)\n"
    m = g.DB_GET.search(src)
    assert m and m.group(1) == "ScheduledEvent" and not _waived(src, m.start())
    waived = "def f(db, x):\n    # tenant-safe: x was loaded with get_owned\n    return db.get(ScheduledEvent, x)\n"
    assert _waived(waived, g.DB_GET.search(waived).start())


def test_the_scheduler_has_no_unscoped_by_id_lookups():
    findings = _findings()
    assert not findings, "Unscoped by-id lookups in app/scheduler:\n  " + "\n  ".join(findings)


def test_the_scheduler_scan_sees_tenant_owned_models():
    """A scan that finds no tenant-owned scheduler model would pass vacuously."""
    import app.main  # noqa: F401

    assert "ScheduledEvent" in _guard()._tenanted_models()
    assert any(SCHEDULER.rglob("router.py"))
