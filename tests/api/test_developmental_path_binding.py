"""Guards on how authored content joins the developmental path.

`CourseModule.po_id` is the only link `qsp_progress` walks to place a learner on the
career map. Binding a module to a performance objective is therefore a CFITES claim
about whether a CAF member is qualified, and it must be explicit, resolvable, and
never inferred. These tests hold that line while keeping the mapping a one-line data
edit for Standards.
"""

import csv
import pathlib

import pytest
import yaml
from app import course_content_ingest, programme_ingest, qsp_ingest, qsp_paths
from app.models import Course, CourseModule, LearningPath

ROOT = pathlib.Path(__file__).resolve().parents[2]
COURSE_DIR = ROOT / "content/courses"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"


@pytest.fixture
def spine(db_session):
    """Ingested QSP spine + programme catalogue + all authored content."""
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    return db_session


def _course_doc(name):
    return yaml.safe_load((COURSE_DIR / name).read_text(encoding="utf-8"))


# -- the mapping must be explicit -----------------------------------------


def test_authored_content_asserts_no_cfites_mapping_by_default(spine):
    """Unsourced content must not claim to deliver a performance objective."""
    bound = spine.query(CourseModule).filter(CourseModule.po_id.isnot(None)).count()
    assert bound == 0, "authored draft content must not assert PO mappings"
    assert spine.query(Course).filter(Course.qualification_id.isnot(None)).count() == 0


def test_any_declared_po_in_a_course_file_exists_in_the_crosswalk():
    """Standards may add mappings; typos and inventions must not survive."""
    with open(CROSSWALK, newline="", encoding="utf-8") as f:
        real = {(r["qsp_code"].strip(), r["po_id"].strip()) for r in csv.DictReader(f)}
    for path in sorted(COURSE_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for m in doc["modules"]:
            ref = m.get("po")
            if not ref:
                continue
            key = (str(ref.get("qsp_code")).strip(), str(ref.get("po_code")).strip())
            assert key in real, f"{path.name} module {m['ordinal']} maps to unknown PO {key}"


# -- binding mechanism ----------------------------------------------------


def test_declared_po_binds_the_module(spine):
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["modules"][0]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_009"}
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["modules_bound_to_po"] == 1


def test_unresolvable_po_is_rejected_not_ignored(spine):
    """A silently-dropped mapping would look wired up while delivering nothing."""
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["modules"][0]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_999"}
    with pytest.raises(ValueError, match="not a performance objective"):
        course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))


def test_unknown_qualification_is_rejected(spine):
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["modules"][0]["po"] = {"qsp_code": "NOPE", "po_code": "PO_009"}
    with pytest.raises(ValueError, match="not in the ingested spine"):
        course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))


def test_course_level_qsp_code_binds_the_qualification(spine):
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["qsp_code"] = "ALJQ"
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["bound_to_qualification"] is True


def test_partial_binding_does_not_infer_the_rest(spine):
    """Mapping one module must not cause the others to be guessed."""
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["modules"][0]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_009"}
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["modules_bound_to_po"] == 1
    assert stats["bound_to_qualification"] is False


# -- coverage report ------------------------------------------------------


def test_coverage_reports_every_objective_uncovered_initially(spine):
    cov = qsp_paths.po_coverage(spine)
    assert cov["objective_count"] == 16
    assert cov["covered"] == 0
    assert cov["uncovered"] == 16
    assert cov["unbound_modules"] == 264


def test_coverage_reflects_a_binding(spine):
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    doc["modules"][0]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_009"}
    course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    cov = qsp_paths.po_coverage(spine)
    assert cov["covered"] == 1
    delivered = [o for o in cov["objectives"] if o["module_count"]]
    assert delivered[0]["po_code"] == "PO_009"
    assert delivered[0]["delivered_by"][0]["course"].startswith("C204")


def test_coverage_is_empty_for_a_tenant_with_no_spine(spine):
    """Tenant scoping: another tenant must not see this tenant's objectives."""
    cov = qsp_paths.po_coverage(spine, tenant_id="00000000-0000-0000-0000-0000000000ff")
    assert cov["objective_count"] == 0


# -- programme delivery paths ---------------------------------------------


def test_programme_paths_are_generated_per_term_and_programme(spine):
    stats = programme_ingest.generate_programme_paths(spine)
    assert stats["term_paths"] == 8
    assert stats["programme_paths"] == 2


def test_programme_paths_are_unpublished(spine):
    programme_ingest.generate_programme_paths(spine)
    assert spine.query(LearningPath).filter(LearningPath.is_published.is_(True)).count() == 0


def test_programme_paths_are_idempotent(spine):
    programme_ingest.generate_programme_paths(spine)
    first = spine.query(LearningPath).count()
    programme_ingest.generate_programme_paths(spine)
    assert spine.query(LearningPath).count() == first


def test_programme_paths_carry_no_qualification_claim(spine):
    """Delivery order only — these must not imply CFITES meaning."""
    programme_ingest.generate_programme_paths(spine)
    for lp in spine.query(LearningPath).all():
        assert "qualification claim" in lp.description
