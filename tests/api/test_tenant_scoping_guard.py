"""Static guard: no unscoped by-id lookup on a tenant-owned model may reach main.

This is the test that stops the bug class coming back. The original leak survived
because a test named `test_cross_tenant_access_denied` asserted `status_code == 200` —
green, well-named, and wrong. Behavioural tests only cover endpoints somebody thought
to write a test for; this one covers every router by construction.

If this fails, do not weaken it. Either route the lookup through
`app.tenancy.get_owned()/owned_or_404()`, add an explicit `tenant_id` predicate, or —
if the query is genuinely scoped through an already-checked parent — mark it with a
`# tenant-safe:` comment explaining WHY.
"""

import re
import sys
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "control-plane/api"
ROUTERS = API / "app/routers"
sys.path.insert(0, str(API))


def _tenanted_models() -> set[str]:
    from app.models import Base

    return {m.class_.__name__ for m in Base.registry.mappers if "tenant_id" in m.class_.__table__.columns}


# db.query(Model) ... .first()/.all()/.one()/.one_or_none()/.scalar()
#
# Until 2026-09-27 this matched only .first()/.all() and only `.id ==`, so the
# SQLAlchemy idiom `filter_by(id=x).one_or_none()` slipped through: qsp.py (template and
# range curriculum, PO links), exercises_collective.py and golden_images.py all leaked
# that way while this guard stayed green.
QUERY = re.compile(r"db\.query\((\w+)\)([\s\S]{0,400}?)\.(?:first|all|one|one_or_none|scalar)\(\)")
# A by-id predicate: `Model.id == x`, `Model.id.in_(...)`, or `filter_by(..., id=x, ...)`.
# `\bid` so `template_id=` (a foreign-key filter) is not mistaken for a primary-key one.
BY_ID = re.compile(r"\.id\s*(==|\.in_)|filter_by\([^)]*\bid\s*=")


def _findings():
    tenanted = _tenanted_models()
    out = []
    for f in sorted(ROUTERS.glob("*.py")):
        # Explicit encoding: Path.read_text() defaults to the platform codec, so on
        # Windows this guard died with UnicodeDecodeError on the first non-ASCII byte
        # in a router rather than reporting findings. Sources are UTF-8 everywhere.
        src = f.read_text(encoding="utf-8")
        for m in QUERY.finditer(src):
            model, body = m.group(1), m.group(2)
            if model not in tenanted:
                continue
            if not BY_ID.search(body):
                continue  # not a by-id lookup
            if "tenant_id" in body:
                continue  # explicitly scoped
            if re.search(r"user\.id|user_id", body):
                continue  # self-scoped (own row)
            line = src[: m.start()].count("\n") + 1
            # allow an explicit, justified waiver in the preceding 4 lines
            before = "\n".join(src.splitlines()[max(0, line - 5) : line])
            if "tenant-safe:" in before:
                continue
            out.append(f"{f.name}:{line} query({model}) by id with no tenant predicate")
    return out


def test_no_unscoped_by_id_lookups_on_tenant_models():
    findings = _findings()
    assert not findings, (
        "Unscoped by-id lookups on tenant-owned models — these return another "
        "tenant's row with a 200 and a well-formed body:\n  "
        + "\n  ".join(findings)
        + "\n\nUse app.tenancy.get_owned(), add a tenant_id filter, or justify with a "
        "'# tenant-safe:' comment."
    )


def test_the_guard_actually_detects_something():
    """A guard that cannot fail is not a guard.

    Confirms the detector fires on a synthetic unscoped lookup, so a future refactor
    that breaks the regex surfaces here rather than silently passing everything.
    """
    tenanted = _tenanted_models()
    assert tenanted, "expected some models to carry tenant_id"
    sample = "x = db.query(Range).filter(Range.id == range_id).first()"
    m = QUERY.search(sample)
    assert m and m.group(1) == "Range", "detector no longer matches the canonical leak"


@pytest.mark.parametrize("sample", [
    "x = db.query(Range).filter(Range.id == range_id).first()",
    "x = db.query(Range).filter_by(id=range_id).one_or_none()",
    "x = db.query(Template).filter_by(id=rng.template_id).one()",
    "x = db.query(Exercise).filter_by(id=exercise_id, kind='collective').one_or_none()",
    "x = db.query(Range).filter(Range.id.in_(ids)).all()",
])
def test_the_guard_detects_every_by_id_idiom(sample):
    m = QUERY.search(sample)
    assert m, f"detector misses: {sample}"
    assert BY_ID.search(m.group(2)), f"not recognised as a by-id lookup: {sample}"


def test_foreign_key_filters_are_not_mistaken_for_by_id_lookups():
    m = QUERY.search("rows = db.query(RangeObjectiveMap).filter_by(template_id=t.id).all()")
    assert m and not BY_ID.search(m.group(2))
