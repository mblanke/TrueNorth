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


def test_every_course_is_bound_to_a_qualification(spine):
    assert spine.query(Course).filter(Course.qualification_id.is_(None)).count() == 0


def test_the_mapping_delivers_every_specified_objective(spine):
    """Every real PO must have a delivering module, so no objective on the career
    map is permanently unreachable. PO_TODO is excluded: it is a needs_spec
    placeholder for the remaining Cpl POs, not an objective anyone can deliver."""
    cov = qsp_paths.po_coverage(spine)
    uncovered = [o["po_code"] for o in cov["objectives"] if not o["module_count"]]
    assert uncovered == ["PO_TODO"], f"objectives with no delivering module: {uncovered}"
    assert cov["covered"] == 15


def test_each_objective_has_exactly_one_delivering_module(spine):
    """qsp_progress takes the first module per PO, so a second mapping would be
    silently ignored and the extra course would not count toward progress."""
    cov = qsp_paths.po_coverage(spine)
    multi = [(o["po_code"], o["module_count"]) for o in cov["objectives"] if o["module_count"] > 1]
    assert not multi, f"objectives with more than one delivering module: {multi}"


def test_a_learner_completing_a_module_advances_the_objective(spine):
    """End to end: authored coursework -> module progress -> position on the path."""
    import uuid

    from app import qsp_progress
    from app.models import Enrollment, EnrollmentStatus, ModuleProgress, ModuleProgressStatus, PerformanceObjective

    po = spine.query(PerformanceObjective).filter_by(po_code="PO_009").one()
    module = spine.query(CourseModule).filter_by(po_id=po.id).one()
    learner = uuid.uuid4()
    enr = Enrollment(user_id=learner, course_id=module.course_id, status=EnrollmentStatus.in_progress)
    spine.add(enr)
    spine.flush()
    spine.add(
        ModuleProgress(
            enrollment_id=enr.id, module_id=module.id, status=ModuleProgressStatus.completed, score=85, max_score=100
        )
    )
    spine.flush()

    mods = {str(module.po_id): module}
    state = qsp_progress.po_progress(spine, learner, [po.id], mods)
    assert state[str(po.id)] == qsp_progress.COMPLETED


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
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["modules_bound_to_po"] == 1
    assert stats["bound_to_qualification"] is True


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
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["bound_to_qualification"] is True


def test_partial_binding_does_not_infer_the_rest(spine):
    """Mapping one module must not cause the others to be guessed."""
    doc = _course_doc("c204-security-monitoring-siem.yaml")
    stats = course_content_ingest.import_course_content(spine, yaml.safe_dump(doc, allow_unicode=True))
    assert stats["modules_bound_to_po"] == 1
    assert len(doc["modules"]) == 6


# -- coverage report ------------------------------------------------------


def test_coverage_shape(spine):
    cov = qsp_paths.po_coverage(spine)
    assert cov["objective_count"] == 16
    assert cov["covered"] + cov["uncovered"] == 16
    assert cov["unbound_modules"] == 264 - cov["covered"]


def test_coverage_names_the_delivering_course_and_module(spine):
    cov = qsp_paths.po_coverage(spine)
    by_po = {o["po_code"]: o for o in cov["objectives"] if o["module_count"]}
    assert by_po["PO_009"]["delivered_by"][0]["course"].startswith("C204")
    assert by_po["PO_009"]["delivered_by"][0]["module"] == "Log Sources and Collection"


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
