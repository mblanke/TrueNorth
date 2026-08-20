"""Guards on how authored content joins the developmental path.

`CourseModule.po_id` is the only link `qsp_progress` walks to place a learner on the
career map. Binding a module to a performance objective is therefore a CFITES claim
about whether a CAF member is qualified, and it must be explicit, resolvable, and
never inferred. These tests hold that line while keeping the mapping a one-line data
edit for Standards.
"""

import json
import csv
import uuid
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


# -- the delivering course is what reaches the path ------------------------


@pytest.fixture
def spine_paths_first(db_session):
    """Spine + generated paths, *then* authored content.

    This is the order a real instance hits: the stubs already hold every objective by
    the time the courses land. `_claim_po` is what makes authored content win.
    """
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    return db_session


@pytest.fixture
def spine_paths_after(db_session):
    """Authored content, *then* generated paths — the reverse order.

    `generate_learning_paths` used to create a fresh stub module at `ordinal=0` for a
    PO an authored module already claimed. Every resolver takes the lowest ordinal, so
    the stub silently shadowed the authored module and real coursework stopped counting.
    """
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)
    return db_session


def _delivering(db, qsp_code, po_code):
    """The course delivering an objective, resolved the way the app resolves it."""
    from app.models import Course, CourseModule, PerformanceObjective, Qualification

    qual = db.query(Qualification).filter_by(qsp_code=qsp_code).one()
    po = db.query(PerformanceObjective).filter_by(qualification_id=qual.id, po_code=po_code).one()
    module = (
        db.query(CourseModule).filter_by(po_id=po.id).order_by(CourseModule.ordinal).first()
    )
    assert module is not None, f"{qsp_code}/{po_code} has no delivering module"
    return db.query(Course).filter_by(id=module.course_id).one(), module


@pytest.mark.parametrize("fixture_name", ["spine_paths_first", "spine_paths_after"])
def test_authored_content_delivers_regardless_of_import_order(fixture_name, request):
    """Neither ordering may leave a stub shadowing authored content."""
    db = request.getfixturevalue(fixture_name)
    course, module = _delivering(db, "ALJQ", "PO_009")
    assert course.name.startswith("C204")
    assert module.title == "Log Sources and Collection"


@pytest.mark.parametrize("fixture_name", ["spine_paths_first", "spine_paths_after"])
def test_one_delivering_module_per_objective_in_both_orders(fixture_name, request):
    db = request.getfixturevalue(fixture_name)
    cov = qsp_paths.po_coverage(db)
    multi = [(o["po_code"], o["module_count"]) for o in cov["objectives"] if o["module_count"] > 1]
    assert not multi, f"objectives with more than one delivering module: {multi}"


def test_generating_paths_after_import_creates_no_duplicate_stubs(spine_paths_after):
    """`_po_course` must return the authored course, not build a rival alongside it."""
    names = [c.name for c in spine_paths_after.query(Course).all()]
    assert len(names) == len(set(names)), "duplicate course names after path generation"


def _path_course_codes(db, name_like):
    lp = db.query(LearningPath).filter(LearningPath.name.like(name_like)).one()
    names = [
        db.query(Course).filter_by(id=cid).one().name for cid in json.loads(lp.course_ids)
    ]
    return [n.split(" — ")[0] for n in names]


def test_the_qualification_path_is_authored_courses(spine_paths_after):
    """The DP1 path is what a learner is told to take — it has to be the real courses."""
    assert _path_course_codes(spine_paths_after, "ALJQ%") == [
        "C105", "C103", "C201", "C209", "C204", "C207",
    ]


def test_importing_content_does_not_recompose_existing_paths(spine_paths_first):
    """Paths are a generated artefact, not a view.

    Importing content removes the superseded stubs from any path that listed them, but
    it does not add the authored courses in their place — that is generation's job. So
    an instance that generated its paths first shows a shorter path until
    `generate-learning-paths` is re-run, which is why the documented chain runs it last.
    """
    assert _path_course_codes(spine_paths_first, "ALJQ%") == []

    qsp_paths.generate_learning_paths(spine_paths_first)

    assert _path_course_codes(spine_paths_first, "ALJQ%") == [
        "C105", "C103", "C201", "C209", "C204", "C207",
    ]


def test_a_course_delivering_several_objectives_appears_once(spine_paths_after):
    """C302 delivers three TEMP64 objectives; a repeat would also self-reference in the
    prerequisite chain."""
    lp = (
        spine_paths_after.query(LearningPath)
        .filter(LearningPath.name.like("TEMP64%"))
        .one()
    )
    course_ids = json.loads(lp.course_ids)
    assert len(course_ids) == len(set(course_ids))
    prereq = json.loads(lp.prerequisite_graph)
    assert not [c for c, deps in prereq.items() if c in deps]


def test_superseded_stubs_are_removed(spine_paths_first):
    """A stub exists only while nothing real delivers its objective.

    Once a course does, keeping the stub means two catalogue entries for one thing.
    Only the unmapped objective's stub survives.
    """
    stubs = [
        c for c in spine_paths_first.query(Course).all()
        if not qsp_paths.course_meta_of(c).get("provenance")
    ]
    assert [c.name.split(" — ")[0] for c in stubs] == ["ACPZ-TODO"]


def test_removing_a_stub_leaves_no_dangling_path_reference(spine_paths_first):
    """`LearningPath.course_ids` is a JSON array with no foreign key, so a deleted
    course would otherwise linger there as an id that resolves to nothing."""
    live = {str(c.id) for c in spine_paths_first.query(Course).all()}
    for lp in spine_paths_first.query(LearningPath).all():
        listed = set(json.loads(lp.course_ids))
        assert listed <= live, f"{lp.name} references courses that no longer exist"
        prereq = json.loads(lp.prerequisite_graph)
        for node, deps in prereq.items():
            assert node in live
            assert set(deps) <= live


def test_a_stub_with_enrolments_is_retired_rather_than_deleted(db_session):
    """Learner history is not ours to discard to tidy a catalogue."""
    from app.models import Enrollment, EnrollmentStatus

    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)

    stub, _ = _delivering(db_session, "ALJQ", "PO_009")
    db_session.add(
        Enrollment(user_id=uuid.uuid4(), course_id=stub.id, status=EnrollmentStatus.in_progress)
    )
    db_session.flush()

    course_content_ingest.import_course_content(
        db_session, (COURSE_DIR / "c204-security-monitoring-siem.yaml").read_text(encoding="utf-8")
    )

    survivor = db_session.query(Course).filter_by(id=stub.id).one_or_none()
    assert survivor is not None, "a stub someone is enrolled on must not be deleted"
    assert qsp_paths.course_meta_of(survivor)["retired"] is True


def test_retired_stubs_stay_off_the_generated_paths(spine_paths_after):
    retired = {
        str(c.id) for c in spine_paths_after.query(Course).all()
        if qsp_paths.course_meta_of(c).get("retired")
    }
    for lp in spine_paths_after.query(LearningPath).all():
        assert not (retired & set(json.loads(lp.course_ids))), f"{lp.name} lists a retired stub"


def test_the_unmapped_objective_keeps_its_stub(spine_paths_first):
    """PO_TODO is a needs_spec placeholder — nothing real delivers it, so the stub
    stays rather than leaving the objective undeliverable."""
    course, _ = _delivering(spine_paths_first, "TEMP67", "PO_TODO")
    assert not qsp_paths.course_meta_of(course).get("provenance")


def test_exercise_wiring_survives_the_content_import(spine_paths_first):
    """The regression this fix exists for.

    The stub held the assess -> scenario -> exercise -> range chain, and superseding it
    released its `po_id` without moving that chain, so every objective on the career map
    lost its exercise deep link. `generate_exercises` has to create the assess row on the
    module that now claims the objective.
    """
    from app.models import ContentKind, ModuleContent

    qsp_paths.generate_exercises(spine_paths_first)
    _, module = _delivering(spine_paths_first, "ALJQ", "PO_009")
    assess = (
        spine_paths_first.query(ModuleContent)
        .filter_by(module_id=module.id, content_kind=ContentKind.assess)
        .one()
    )
    assert assess.scenario_id is not None


# -- where a course renders on the developmental path ----------------------

TAXONOMY = ROOT / "content/catalogue/nist_csf_2_0_taxonomy.csv"
COMPETENCY = ROOT / "content/catalogue/qsp_competency_crosswalk.csv"


@pytest.fixture
def spine_placed(db_session):
    """The full chain, including the competency crosswalk.

    `import_competency_crosswalk` is what stamps `dp_order`, `track` and `rank_level`
    onto each qualification. Without it every qualification sits at DP0 and the map has
    no columns to place anything in, so placement cannot be tested without it.
    """
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    qsp_paths.import_competency_crosswalk(
        db_session,
        TAXONOMY.read_text(encoding="utf-8"),
        COMPETENCY.read_text(encoding="utf-8"),
    )
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)
    return db_session


def _codes(courses):
    return sorted(c["course_code"] for c in courses)


def test_dp1_is_the_algonquin_programme(spine_placed):
    """DP1 is the Algonquin College programme — all 32 of it, not only the courses
    that happen to deliver an objective."""
    grouped = qsp_paths.programme_courses(spine_placed)
    dp1 = grouped[(1, "progression")]
    assert len(dp1) == 32
    assert {c["institution"] for c in dp1} == {"Algonquin College"}


def test_dp2_is_the_rmc_programme(spine_placed):
    grouped = qsp_paths.programme_courses(spine_placed)
    dp2 = [c for (dp, _), courses in grouped.items() if dp == 2 for c in courses]
    assert len(dp2) == 12
    assert {c["institution"] for c in dp2} == {"Royal Military College"}


def test_dp2_splits_across_its_specialty_lanes(spine_placed):
    """Within a period the lane is the course's own qualification."""
    grouped = qsp_paths.programme_courses(spine_placed)
    assert _codes(grouped[(2, "ALRA")]) == ["RMC C204", "RMC C207"]
    assert _codes(grouped[(2, "ALRA-RED")]) == ["RMC C205"]
    assert len(grouped[(2, "progression")]) == 9


def test_a_course_stays_in_the_period_that_teaches_it(spine_placed):
    """C302/C305/C306 are Algonquin Year-3 courses bound to DP2 qualifications.

    Placement follows the catalogue, so they render on DP1 where they are actually
    taught, rather than being pulled forward into a period the learner has not reached.
    """
    grouped = qsp_paths.programme_courses(spine_placed)
    dp1 = {c["course_code"]: c for c in grouped[(1, "progression")]}
    cross = ("C302", "C305", "C306", "C208")
    for code in cross:
        assert code in dp1
        assert dp1[code]["delivers_cross_dp"] is True
    for lane in ("ALRA", "ALRA-RED"):
        assert not [c for c in grouped[(2, lane)] if c["course_code"] in cross]


def test_cross_dp_courses_still_name_what_they_deliver(spine_placed):
    """Placement moving does not weaken the delivery claim."""
    grouped = qsp_paths.programme_courses(spine_placed)
    c302 = next(c for c in grouped[(1, "progression")] if c["course_code"] == "C302")
    assert sorted(d["po_code"] for d in c302["delivers"]) == ["PO_001", "PO_002", "PO_003"]
    assert {d["qsp_code"] for d in c302["delivers"]} == {"TEMP64"}


def test_most_of_the_programme_delivers_nothing(spine_placed):
    """The honest shape of the catalogue: 32 courses taught in DP1, 10 of them
    delivering a performance objective and 22 delivering none."""
    grouped = qsp_paths.programme_courses(spine_placed)
    dp1 = grouped[(1, "progression")]
    delivering = [c for c in dp1 if c["delivers"]]
    assert _codes(delivering) == [
        "C103", "C105", "C201", "C204", "C207", "C208", "C209", "C302", "C305", "C306",
    ]
    assert len(dp1) - len(delivering) == 22


def test_retired_stubs_are_not_programme_courses(spine_placed):
    grouped = qsp_paths.programme_courses(spine_placed)
    everything = [c for courses in grouped.values() for c in courses]
    assert len(everything) == 44
    assert all(c["course_code"] for c in everything)


def test_the_delivery_tag_matches_actual_coverage(spine_placed):
    """`delivers:*` is derived from the crosswalk binding, never from the file's own
    `qsp_code` — asserting delivery is a CFITES claim."""
    cov = {
        o["po_code"]: o for o in qsp_paths.po_coverage(spine_placed)["objectives"]
    }
    assert cov  # sanity
    for course in spine_placed.query(Course).all():
        meta = qsp_paths.course_meta_of(course)
        if meta.get("retired") or not meta.get("programme"):
            continue
        tags = set(json.loads(course.tags or "[]"))
        delivered = programme_ingest.delivered_qsp_codes(spine_placed, course.id)
        expected = {f"delivers:{c.lower()}" for c in delivered} or {"delivers:none"}
        assert {t for t in tags if t.startswith("delivers:")} == expected


def test_every_catalogue_course_carries_its_dp_tag(spine_placed):
    for course in spine_placed.query(Course).all():
        meta = qsp_paths.course_meta_of(course)
        if meta.get("retired") or not meta.get("programme"):
            continue
        assert f"DP{meta['dp_order']}" in json.loads(course.tags or "[]")


def test_first_year_courses_are_beginner():
    """Year 1 of DP1 is the entry point — it must not present as intermediate.

    Difficulty is authored in the course file rather than derived, so nothing stops a
    foundations course from drifting up a level; this pins the rule to the catalogue's
    own term codes instead of a hand-kept list.
    """
    terms = {
        r["course_code"]: r["term_code"]
        for r in csv.DictReader(CATALOGUE.open(encoding="utf-8"))
    }
    wrong = []
    for path in sorted(COURSE_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        term = terms.get(doc.get("course_code"), "")
        if term.startswith("DP1-Y1") and doc.get("difficulty") != "beginner":
            wrong.append((doc.get("course_code"), doc.get("difficulty")))
    assert not wrong, f"first-year courses not marked beginner: {wrong}"


def test_difficulty_survives_import(spine):
    """The authored level has to reach the catalogue, not just the file."""
    c101 = next(c for c in spine.query(Course).all() if c.name.startswith("C101"))
    assert c101.difficulty == "beginner"


# -- authored modules carry the same structure the stubs did ---------------


def test_every_authored_module_has_teaching_content(spine):
    """A module's objectives, topics, lab and citations lived in a JSON blob nothing
    renders, so a 100-hour authored course displayed as an empty shell beside a
    placeholder. Every module now projects them into a Lesson."""
    from app.models import ContentKind, Lesson, ModuleContent

    modules = spine.query(CourseModule).all()
    assert len(modules) == 264
    missing = []
    for mod in modules:
        teach = (
            spine.query(ModuleContent)
            .filter_by(module_id=mod.id, content_kind=ContentKind.teach)
            .one_or_none()
        )
        if teach is None or teach.lesson_id is None:
            missing.append(mod.title)
            continue
        lesson = spine.query(Lesson).filter_by(id=teach.lesson_id).one()
        if not lesson.body_markdown.strip():
            missing.append(mod.title)
    assert not missing, f"modules with no teaching content: {missing[:5]}"


def test_quizzes_are_reachable_as_module_content(spine):
    """264 quizzes existed but nothing linked them into the teach/check/assess flow."""
    from app.models import ContentKind, ModuleContent, Quiz

    quizzes = spine.query(Quiz).count()
    checks = spine.query(ModuleContent).filter_by(content_kind=ContentKind.check).count()
    assert quizzes and checks == quizzes


def test_teaching_content_is_never_published(spine):
    """Authored draft content is not learner-facing, whatever shape it takes."""
    from app.models import Lesson

    assert spine.query(Lesson).filter(Lesson.is_published.is_(True)).count() == 0


def test_module_content_is_idempotent(spine):
    """Re-import must update the content rows, not accumulate a second set."""
    from app.models import ModuleContent

    before = spine.query(ModuleContent).count()
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(spine, f.read_text(encoding="utf-8"))
    assert spine.query(ModuleContent).count() == before


def test_generated_exercises_keep_their_scenario_on_reimport(spine):
    """`generate_exercises` owns `assess.scenario_id`; re-importing content must not
    wipe the wiring it created."""
    from app.models import ContentKind, ModuleContent

    qsp_paths.generate_exercises(spine)
    _, module = _delivering(spine, "ALJQ", "PO_009")
    before = (
        spine.query(ModuleContent)
        .filter_by(module_id=module.id, content_kind=ContentKind.assess)
        .one()
        .scenario_id
    )
    assert before is not None

    course_content_ingest.import_course_content(
        spine, (COURSE_DIR / "c204-security-monitoring-siem.yaml").read_text(encoding="utf-8")
    )
    after = (
        spine.query(ModuleContent)
        .filter_by(module_id=module.id, content_kind=ContentKind.assess)
        .one()
        .scenario_id
    )
    assert after == before


def test_stubs_released_by_an_earlier_run_are_purged(db_session):
    """Superseding releases a stub's objective; nothing was removing the stub itself.

    A stub released by an earlier import holds no objective and appears on no path, so
    it is a dead row that still shows up in the catalogue. Path generation is where the
    spine is reconciled, so that is where it gets cleaned up.
    """
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)

    # Release a stub the way a previous version of the importer did, without deleting it.
    stub, module = _delivering(db_session, "ALJQ", "PO_009")
    module.po_id = None
    db_session.flush()
    assert db_session.query(Course).filter_by(id=stub.id).one_or_none() is not None

    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    stats = qsp_paths.generate_learning_paths(db_session)

    assert stats["stubs_purged"] >= 1
    assert db_session.query(Course).filter_by(id=stub.id).one_or_none() is None


def test_the_purge_spares_the_objective_nothing_delivers(db_session):
    """PO_TODO has no real deliverer, so its stub is the only thing standing between
    that objective and being undeliverable. It must survive."""
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))
    qsp_paths.generate_learning_paths(db_session)
    qsp_paths.purge_orphaned_stubs(db_session)

    course, _ = _delivering(db_session, "TEMP67", "PO_TODO")
    assert course.name.startswith("ACPZ-TODO")


def test_the_purge_never_touches_authored_content(db_session):
    qsp_ingest.import_crosswalk(db_session, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    for f in sorted(COURSE_DIR.glob("*.yaml")):
        course_content_ingest.import_course_content(db_session, f.read_text(encoding="utf-8"))

    before = {
        c.name for c in db_session.query(Course).all()
        if qsp_paths.course_meta_of(c).get("provenance")
    }
    qsp_paths.purge_orphaned_stubs(db_session)
    after = {
        c.name for c in db_session.query(Course).all()
        if qsp_paths.course_meta_of(c).get("provenance")
    }
    assert before == after and len(before) == 44
