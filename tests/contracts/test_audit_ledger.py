# Author: Marc-Andre Blanke
# Created: 2026-11-18T19:00:00Z
"""docs/audit-443-ledger.md names real oracle tests (last-mile 2.12).

Every `test_...` name in the ledger exists in the test tree, and every strict-xfail audit
test is named in full in the ledger, so a finding cannot be added or left unrecorded without
this failing. (A finding that is fixed turns its strict xfail into a failing XPASS, which is
what forces the marker, and then the ledger row, out.)
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs/audit-443-ledger.md"
AUDIT_FILES = (
    list((ROOT / "tests/api").glob("test_audit*_443.py"))
    + list((ROOT / "tests/worker").glob("test_audit*_443.py"))
    + [ROOT / "tests/api/test_search_old_index.py"]
)


def _all_test_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        names.update(re.findall(r"^\s*(?:async )?def (test_\w+)", path.read_text(encoding="utf-8"), re.M))
    return names


def _open_xfail_names() -> set[str]:
    found: set[str] = set()
    for path in AUDIT_FILES:
        s = path.read_text(encoding="utf-8")
        for m in re.finditer(r"@pytest\.mark\.xfail\(.*?\)\s*\n\s*(?:async )?def (test_\w+)", s, re.S):
            found.add(m.group(1))
    return found


def test_every_test_the_ledger_names_exists():
    text = LEDGER.read_text(encoding="utf-8")
    named = {n for n in re.findall(r"`(test_\w+)`", text)}
    missing = sorted(named - _all_test_names())
    assert not missing, f"the ledger names tests that do not exist: {missing}"


def test_every_open_audit_test_is_in_the_ledger():
    text = LEDGER.read_text(encoding="utf-8")
    open_tests = _open_xfail_names()
    assert open_tests, "no strict-xfail audit tests found: update the ledger's claim"
    absent = sorted(n for n in open_tests if f"`{n}`" not in text)
    assert not absent, f"open audit tests the ledger does not list: {absent}"
