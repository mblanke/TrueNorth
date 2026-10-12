"""ADR 0010: no NULL-on-delete beyond the sites that predate it.

A delete that would orphan history is refused with 409 (``app/delete_guard.py``,
``tests/api/test_delete_conflicts.py``); a record nobody wants is retired, not deleted.
Nulling a reference to let a delete through is ruled out. This guard lists every
``ondelete="SET NULL"`` foreign key and every bulk ``update({col: None})`` in the API and
fails on one that is not in the ADR's table of exceptions.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "control-plane/api/app"

SET_NULL = re.compile(r"""ondelete\s*=\s*["']SET NULL["']""", re.I)
BULK_NULL = re.compile(r"\.update\(\s*\{[^{}]*:\s*None\s*\}")

# docs/adr/0010-delete-policy.md, "Exceptions that predate this ADR". Shrink, never grow.
GRANDFATHERED = {
    ("scenario_runs/models.py", "set-null"): 2,  # scenario_executions.scenario_id, range_id
    ("scheduler/service.py", "bulk-null"): 1,  # detach(): finished events lose the link
    ("routers/courses.py", "bulk-null"): 1,  # delete_course: quizzes.module_id
}


def _sites() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in APP.rglob("*.py"):
        text = path.read_text()
        rel = path.relative_to(APP).as_posix()
        for kind, pattern in (("set-null", SET_NULL), ("bulk-null", BULK_NULL)):
            if n := len(pattern.findall(text)):
                found[(rel, kind)] = n
    return found


def test_no_new_null_on_delete():
    assert _sites() == GRANDFATHERED, (
        "A reference is nulled outside ADR 0010's exceptions. Refuse the delete with 409 "
        "(delete_guard.refuse_if) and give the record a retire/archive state instead."
    )


def test_the_adr_lists_the_exceptions():
    adr = (Path(__file__).resolve().parents[2] / "docs/adr/0010-delete-policy.md").read_text()
    for rel, _kind in GRANDFATHERED:
        assert rel in adr, rel
