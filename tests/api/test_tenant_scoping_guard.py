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
APP = API / "app"
sys.path.insert(0, str(API))


def _sources() -> list[Path]:
    """Every API module, not only routers: section services (lab_sessions, course
    publishing, the QSP generators) take ids from requests and other rows too. Until
    2026-10-08 only app/routers/*.py was scanned, and 39 unexamined lookups sat outside
    it; tracing them found three real leaks (qsp_paths generation, course-content PO
    claims, learning-path enrollment). Model and migration files hold no queries."""
    return sorted(
        f for f in APP.rglob("*.py") if f.name != "models.py" and "alembic" not in f.relative_to(APP).parts
    )


def _name(f: Path) -> str:
    return f.relative_to(APP).as_posix()


def _tenanted_models() -> set[str]:
    import importlib

    from app.models import Base

    # Section modules (app/<section>/models.py: lab_sessions, course_releases, ...) map
    # onto the same Base but only register once imported. Without this the set depended
    # on whether another test had imported app.main first, so a run of this file alone
    # missed LabSession and CourseRelease.
    for mod in sorted((API / "app").glob("*/models.py")):
        importlib.import_module(f"app.{mod.parent.name}.models")
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
    for f in _sources():
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
            out.append(f"{_name(f)}:{line} query({model}) by id with no tenant predicate")
    return out


# Known unscoped by-id lookups (both detectors), per module under app/. A ratchet: a
# count may go down (lower it here), never up, and a module not listed must have none.
#
# Reviewed 2026-10-08 (security sweep M4), when the scan widened from routers to app/**:
# 39 hits. Real leaks, fixed with tests: qsp_paths.py (generate_learning_paths and
# generate_exercises matched modules, scenarios, ranges, templates, exercises and paths
# across tenants), course_content_ingest.py (_claim_po superseded other tenants'
# modules), enrollment.py (a learning path, and its courses, from another tenant), and
# rbac.require_range_access/require_tenant_access (any tenant's admin bypassed them;
# now only the platform admin). The rest follow a row's own foreign keys from a parent
# the caller already holds (a lab session's range, a publication's release) and are
# waived in place. The ratchet is empty.
SOURCE_BASELINE: dict[str, int] = {}


def test_no_unscoped_by_id_lookups_on_tenant_models():
    counts: dict[str, int] = {}
    for hit in _findings():
        counts[hit.split(":", 1)[0]] = counts.get(hit.split(":", 1)[0], 0) + 1
    findings = [h for h in _findings() if counts[h.split(":", 1)[0]] > SOURCE_BASELINE.get(h.split(":", 1)[0], 0)]
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


# `db.get(Model, id)` is a primary-key lookup with no room for a tenant predicate, and
# the QUERY pattern above never saw it: directory.py and auth_zones.py did every by-id
# fetch that way and leaked across tenants while this guard stayed green (fixed
# 2026-10-07). A `db.get` of a tenant-owned model needs a `# tenant-safe:` waiver in
# the preceding 4 lines, or should be `app.tenancy.get_owned()`.
DB_GET = re.compile(r"db\.get\((\w+)\s*,")

# Known unscoped `db.get` calls, per router file. A ratchet: a count may go down (lower
# it here), never up, and a file not listed must have none.
#
# Reviewed 2026-10-07 (tenancy follow-up); every other file is at 0:
# - hypervisors.py (6): real leaks, fixed with get_owned.
# - ai_config.py (6): platform-wide config behind a platform-admin-only router; one
#   waived helper.
# - lab_sessions.py (3), curriculum.py (2), integrations.py (1): already scoped (tenant
#   compared on the next line, signed lab/deep-link token, or ids from a handler that
#   passed get_owned); waived in place.
# - quizzes.py (1): the quiz of an attempt fetched by the caller's user_id; waived.
# The ratchet is empty: every router must now have none. Since 2026-10-08 the scan covers
# every module under app/ (keys are paths relative to it, e.g. "routers/hypervisors.py");
# see SOURCE_BASELINE for that review.
DB_GET_BASELINE: dict[str, int] = {}


def _db_get_findings() -> dict[str, list[str]]:
    tenanted = _tenanted_models()
    out: dict[str, list[str]] = {}
    for f in _sources():
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        for m in DB_GET.finditer(src):
            if m.group(1) not in tenanted:
                continue
            line = src[: m.start()].count("\n") + 1
            if "tenant-safe:" in "\n".join(lines[max(0, line - 5) : line]):
                continue
            out.setdefault(_name(f), []).append(f"{_name(f)}:{line} db.get({m.group(1)}, ...)")
    return out


def test_no_new_unscoped_db_get_on_tenant_models():
    over = []
    for name, hits in _db_get_findings().items():
        allowed = DB_GET_BASELINE.get(name, 0)
        if len(hits) > allowed:
            over.append(f"{name}: {len(hits)} > baseline {allowed}\n    " + "\n    ".join(hits))
    assert not over, (
        "db.get() of a tenant-owned model returns another tenant's row by id:\n  "
        + "\n  ".join(over)
        + "\n\nUse app.tenancy.get_owned(), or justify with a '# tenant-safe:' comment."
    )


@pytest.mark.parametrize(
    "router",
    [
        "directory.py", "auth_zones.py", "storage.py", "ad_sync.py", "admin.py",
        # reviewed in the 2026-10-07 tenancy follow-up
        "ai_config.py", "hypervisors.py", "lab_sessions.py", "curriculum.py", "integrations.py",
        "quizzes.py",
    ],
)
def test_platform_routers_have_no_unscoped_db_get(router):
    """These routers were fixed or reviewed outright; they carry no baseline."""
    key = f"routers/{router}"
    assert (APP / key).is_file(), key
    assert key not in DB_GET_BASELINE
    assert not _db_get_findings().get(key), _db_get_findings()[key]


@pytest.mark.parametrize(
    "module",
    [
        "lab_sessions/service.py", "course_publishing/service.py", "course_publishing/runner.py",
        "course_releases/service.py", "course_content_ingest.py", "qsp_paths.py", "enrollment.py",
        "range_lifecycle.py", "rbac.py",
    ],
)
def test_reviewed_service_modules_carry_no_baseline(module):
    """Traced in the 2026-10-08 sweep: fixed or waived in place, never baselined."""
    assert (APP / module).is_file(), module
    assert module not in SOURCE_BASELINE and module not in DB_GET_BASELINE
    assert not [h for h in _findings() if h.startswith(module + ":")]
    assert not _db_get_findings().get(module)


def test_the_scan_covers_service_modules_and_skips_models():
    names = {_name(f) for f in _sources()}
    assert {"routers/ranges.py", "lab_sessions/service.py", "qsp_paths.py"} <= names
    assert not any(n.endswith("models.py") or n.startswith("alembic/") for n in names)


def test_the_db_get_detector_fires():
    assert DB_GET.search("ou = db.get(OrganizationalUnit, str(ou_id))").group(1) == "OrganizationalUnit"
    assert "OrganizationalUnit" in _tenanted_models()


def test_foreign_key_filters_are_not_mistaken_for_by_id_lookups():
    m = QUERY.search("rows = db.query(RangeObjectiveMap).filter_by(template_id=t.id).all()")
    assert m and not BY_ID.search(m.group(2))
