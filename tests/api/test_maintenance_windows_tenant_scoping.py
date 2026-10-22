"""The tenant-scoping guard, applied to the maintenance window section (ADR 0003).

tests/api/test_tenant_scoping_guard.py scans ``app/routers/*.py`` only; maintenance windows
live in their own section package, so this runs the same two detectors over
``app/maintenance_windows/`` with no baseline: every by-id lookup of a tenant-owned model
carries a tenant predicate or a justified ``# tenant-safe:`` waiver.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECTION = ROOT / "control-plane/api/app/maintenance_windows"


def _guard():
    spec = importlib.util.spec_from_file_location("_tenant_guard", ROOT / "tests/api/test_tenant_scoping_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(src: str, start: int) -> int:
    return src[:start].count("\n") + 1


def _waived(src: str, start: int) -> bool:
    line = _line(src, start)
    return "tenant-safe:" in "\n".join(src.splitlines()[max(0, line - 5) : line])


def test_the_section_has_no_unscoped_by_id_lookups():
    import app.main  # noqa: F401  every model registered

    g = _guard()
    tenanted = g._tenanted_models()
    assert "MaintenanceWindow" in tenanted, "a scan that sees no tenant-owned model passes vacuously"
    out: list[str] = []
    for f in sorted(SECTION.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in g.QUERY.finditer(src):
            model, body = m.group(1), m.group(2)
            if model not in tenanted or not g.BY_ID.search(body) or "tenant_id" in body:
                continue
            if re.search(r"user\.id|user_id", body) or _waived(src, m.start()):
                continue
            out.append(f"{f.name}:{_line(src, m.start())} query({model}) by id")
        for m in g.DB_GET.finditer(src):
            if m.group(1) in tenanted and not _waived(src, m.start()):
                out.append(f"{f.name}:{_line(src, m.start())} db.get({m.group(1)})")
    assert not out, "Unscoped by-id lookups in app/maintenance_windows:\n  " + "\n  ".join(out)
