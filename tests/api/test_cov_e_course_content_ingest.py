# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/course_content_ingest.py: spine bindings, placeholders, releases.

The existing course-content tests guard the authored YAML files themselves; these exercise
the importer's behaviour against real rows — ``po:`` bindings resolved against a seeded
spine, placeholder courses superseded or retired, release-mode alignment and quiz
retirement — plus the parser's lenient edges. Nothing is stubbed; the in-memory database
is the only collaborator.
"""

import json
import uuid

import pytest
import yaml
from app.course_content_ingest import (
    answer_indices,
    find_catalogue_course,
    import_course_content,
    parse_course_content,
)
from app.models import (
    ContentKind,
    Course,
    CourseModule,
    Enrollment,
    ModuleContent,
    ModuleContentType,
    PerformanceObjective,
    Qualification,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    User,
    UserRole,
)

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")


# -- builders -----------------------------------------------------------------


def spine(db, qsp="TSXX", po_code="PO_001"):
    """A minimal ingested crosswalk: one qualification with one performance objective."""
    q = Qualification(qsp_code=qsp, nqual=qsp, title="Test standard")
    db.add(q)
    db.flush()
    po = PerformanceObjective(qualification_id=q.id, po_code=po_code, title="Do the thing")
    db.add(po)
    db.flush()
    return q, po


def catalogue_course(db, code, *, provenance=None):
    meta = {"course_code": code}
    if provenance:
        meta["provenance"] = provenance
    c = Course(
        id=uuid.uuid4(), name=f"{code} catalogue entry", version="1.0", tenant_id=DEV, course_meta=json.dumps(meta)
    )
    db.add(c)
    db.flush()
    return c


def placeholder(db, code, po):
    """A spine-generated stub: a catalogue course with a module bound to the PO, no content."""
    c = catalogue_course(db, code)
    m = CourseModule(
        id=uuid.uuid4(),
        course_id=c.id,
        ordinal=1,
        title=f"{code} stub",
        content_type=ModuleContentType.reading,
        po_id=po.id,
    )
    db.add(m)
    db.add(ModuleContent(module_id=m.id, content_kind=ContentKind.teach))
    db.flush()
    return c, m


def student(db):
    u = User(
        id=uuid.uuid4(),
        email=f"s{uuid.uuid4().hex[:6]}@example.test",
        display_name="Student",
        role=UserRole.student,
        tenant_id=DEV,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.flush()
    return u


def module_doc(ordinal, **over):
    m = {
        "ordinal": ordinal,
        "title": f"Module {ordinal}",
        "activity": "theory",
        "objectives": [f"Objective {ordinal}"],
        "topics": [f"Topic {ordinal}"],
        "duration_minutes": 30,
        "quiz": {
            "title": f"Check {ordinal}",
            "pass_threshold": 70,
            "questions": [
                {"question": f"Pick one for module {ordinal}", "options": ["A) alpha", "B) beta"], "answer": "A"},
            ],
        },
    }
    m.update(over)
    return m


def course_doc(code, modules, **over):
    doc = {
        "course_code": code,
        "title": f"Cover course {code}",
        "version": "1.0",
        "duration_hours": 2,
        "provenance": "authored",
        "modules": modules,
    }
    doc.update(over)
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def the_course(db, code):
    return db.query(Course).filter_by(name=f"{code} catalogue entry").one()


# -- parser edges ---------------------------------------------------------------


def test_option_prefixes_are_stripped_and_unprefixed_options_pass_through():
    doc = course_doc(
        "OPT-C1",
        [
            module_doc(
                1,
                quiz={
                    "title": "q",
                    "questions": [
                        {
                            "question": "Q",
                            "options": ["MQTT broker", "B) prefixed", "A)", "AB) two chars"],
                            "answer": "A",
                        },
                    ],
                },
            )
        ],
    )
    parsed = parse_course_content(doc)
    assert parsed["modules"][0]["questions"][0]["options"] == [
        "MQTT broker",
        "prefixed",
        "A)",
        "AB) two chars",
    ]


def test_a_po_binding_is_normalised_and_partial_ones_are_dropped():
    doc = course_doc(
        "POREF-C1",
        [
            module_doc(1, po={"qsp_code": " TSXX ", "po_code": " PO_001 "}),
            module_doc(2, po={"qsp_code": "TSXX"}),
            module_doc(3, po="not a dict"),
            module_doc(4, po={"qsp_code": "", "po_code": "PO_001"}),
        ],
    )
    pos = [m["po"] for m in parse_course_content(doc)["modules"]]
    assert pos == [
        {"qsp_code": "TSXX", "po_code": "PO_001"},
        None,
        None,
        None,
    ]


def test_answer_indices_accept_semicolons_and_deduplicate():
    assert answer_indices("b ; a, B", ["x", "y"]) == [0, 1]
    assert answer_indices("AA", ["x", "y"]) == []


# -- resolving a declared po: binding against the spine ------------------------------


def test_a_declared_po_binds_the_module_to_the_spine_row(db_session):
    spine(db_session)
    catalogue_course(db_session, "BIND-C1")
    doc = course_doc("BIND-C1", [module_doc(1, po={"qsp_code": "TSXX", "po_code": "PO_001"})])
    stats = import_course_content(db_session, doc, DEV)
    module = db_session.query(CourseModule).one()
    assert db_session.get(PerformanceObjective, module.po_id).po_code == "PO_001"
    assert stats["modules_bound_to_po"] == 1


def test_a_po_binding_to_an_unknown_qsp_is_refused(db_session):
    catalogue_course(db_session, "ORPH-C1")
    doc = course_doc("ORPH-C1", [module_doc(1, po={"qsp_code": "NOPE", "po_code": "PO_001"})])
    with pytest.raises(ValueError, match="maps to qsp_code='NOPE'.*import crosswalk.csv first"):
        import_course_content(db_session, doc, DEV)


def test_a_po_binding_to_an_unknown_po_code_is_refused(db_session):
    spine(db_session)
    catalogue_course(db_session, "ORPH-C2")
    doc = course_doc("ORPH-C2", [module_doc(1, po={"qsp_code": "TSXX", "po_code": "PO_999"})])
    with pytest.raises(ValueError, match="not a performance objective in the crosswalk"):
        import_course_content(db_session, doc, DEV)


def test_a_course_qsp_code_outside_the_spine_is_refused(db_session):
    catalogue_course(db_session, "ORPH-C3")
    doc = course_doc("ORPH-C3", [module_doc(1)], qsp_code="NOPE")
    with pytest.raises(ValueError, match="declares qsp_code='NOPE'"):
        import_course_content(db_session, doc, DEV)


# -- locating the catalogue course ---------------------------------------------------


def test_a_course_with_unreadable_meta_is_skipped_when_locating_the_catalogue(db_session):
    bad = catalogue_course(db_session, "META-C1")
    bad.course_meta = "{oops"
    good = catalogue_course(db_session, "META-C1")
    found = find_catalogue_course(db_session, "META-C1", DEV)
    assert found is not None and found.id == good.id


# -- placeholders: authored content supersedes spine-generated stubs -----------------


def test_authored_content_supersedes_a_placeholder_and_deletes_it(db_session):
    _, po = spine(db_session)
    stub, stub_module = placeholder(db_session, "STUB-C1", po)
    catalogue_course(db_session, "AUTH-C1")
    doc = course_doc("AUTH-C1", [module_doc(1, po={"qsp_code": "TSXX", "po_code": "PO_001"})])
    stats = import_course_content(db_session, doc, DEV)
    assert stats["placeholders_superseded"] == 1
    assert stats["placeholders_deleted"] == 1
    assert db_session.get(Course, stub.id) is None
    assert db_session.get(CourseModule, stub_module.id) is None
    remaining = db_session.query(CourseModule).all()
    assert len(remaining) == 1 and remaining[0].po_id == po.id


def test_an_enrolled_placeholder_is_retired_not_deleted(db_session):
    _, po = spine(db_session)
    stub, _ = placeholder(db_session, "STUB-C2", po)
    learner = student(db_session)
    db_session.add(Enrollment(user_id=learner.id, course_id=stub.id, tenant_id=DEV))
    db_session.flush()
    catalogue_course(db_session, "AUTH-C2")
    doc = course_doc("AUTH-C2", [module_doc(1, po={"qsp_code": "TSXX", "po_code": "PO_001"})])
    stats = import_course_content(db_session, doc, DEV)
    assert stats["placeholders_retired"] == 1
    assert stats.get("placeholders_deleted", 0) == 0
    survivor = db_session.get(Course, stub.id)
    assert survivor is not None
    meta = json.loads(survivor.course_meta)
    assert meta["retired"] is True
    assert meta["superseded_by"] == "AUTH-C2"


def test_two_authored_courses_may_not_claim_one_po(db_session):
    _, po = spine(db_session)
    first = catalogue_course(db_session, "FIRST-C1", provenance="programme")
    db_session.add(
        CourseModule(
            id=uuid.uuid4(),
            course_id=first.id,
            ordinal=1,
            title="First claim",
            content_type=ModuleContentType.reading,
            po_id=po.id,
        )
    )
    db_session.flush()
    catalogue_course(db_session, "SECOND-C1")
    doc = course_doc("SECOND-C1", [module_doc(1, po={"qsp_code": "TSXX", "po_code": "PO_001"})])
    with pytest.raises(ValueError, match="claims a performance objective already delivered by authored course"):
        import_course_content(db_session, doc, DEV)


# -- release mode: replacing content students are using ------------------------------


def test_release_mode_retires_modules_the_release_dropped(db_session):
    catalogue_course(db_session, "REL-C1")
    import_course_content(db_session, course_doc("REL-C1", [module_doc(1), module_doc(2)]), DEV)
    course = the_course(db_session, "REL-C1")
    dropped = db_session.query(CourseModule).filter_by(course_id=course.id, ordinal=2).one()
    dropped.content_ref = "{oops"  # an unreadable ref still retires cleanly
    db_session.flush()
    stats = import_course_content(db_session, course_doc("REL-C1", [module_doc(1)]), DEV, release_mode=True)
    assert stats["modules_retired"] == 1
    assert dropped.is_required is False
    assert json.loads(dropped.content_ref) == {"retired": True}


def test_release_mode_gives_every_enrollment_progress_rows_for_current_modules(db_session):
    catalogue_course(db_session, "REL-C2")
    import_course_content(db_session, course_doc("REL-C2", [module_doc(1), module_doc(2)]), DEV)
    course = the_course(db_session, "REL-C2")
    learner = student(db_session)
    db_session.add(Enrollment(user_id=learner.id, course_id=course.id, tenant_id=DEV))
    db_session.flush()
    doc = course_doc("REL-C2", [module_doc(1), module_doc(2)])
    stats = import_course_content(db_session, doc, DEV, release_mode=True)
    assert stats["progress_rows_added"] == 2
    again = import_course_content(db_session, doc, DEV, release_mode=True)
    assert again.get("progress_rows_added", 0) == 0


def test_release_mode_retires_an_attempted_quiz_whose_questions_changed(db_session):
    catalogue_course(db_session, "REL-C3")
    import_course_content(db_session, course_doc("REL-C3", [module_doc(1)]), DEV)
    module = db_session.query(CourseModule).filter_by(course_id=the_course(db_session, "REL-C3").id).one()
    old_quiz = db_session.query(Quiz).filter_by(module_id=module.id).one()
    learner = student(db_session)
    db_session.add(QuizAttempt(quiz_id=old_quiz.id, user_id=learner.id, tenant_id=DEV))
    db_session.flush()
    changed = course_doc(
        "REL-C3",
        [
            module_doc(
                1,
                quiz={
                    "title": "Check 1",
                    "pass_threshold": 70,
                    "questions": [{"question": "Brand new stem", "options": ["A) yes", "B) no"], "answer": "B"}],
                },
            )
        ],
    )
    stats = import_course_content(db_session, changed, DEV, release_mode=True)
    assert stats["quizzes_retired"] == 1
    retired = db_session.get(Quiz, old_quiz.id)
    assert retired.title == "Check 1 (retired)"
    assert retired.is_published is False
    new_quiz = db_session.query(Quiz).filter(Quiz.module_id == module.id, Quiz.id != old_quiz.id).one()
    assert [q.stem for q in new_quiz.questions] == ["Brand new stem"]


def test_release_mode_leaves_an_unchanged_attempted_quiz_in_place(db_session):
    catalogue_course(db_session, "REL-C4")
    doc = course_doc("REL-C4", [module_doc(1)])
    import_course_content(db_session, doc, DEV)
    module = db_session.query(CourseModule).filter_by(course_id=the_course(db_session, "REL-C4").id).one()
    quiz = db_session.query(Quiz).filter_by(module_id=module.id).one()
    learner = student(db_session)
    db_session.add(QuizAttempt(quiz_id=quiz.id, user_id=learner.id, tenant_id=DEV))
    db_session.flush()
    stats = import_course_content(db_session, doc, DEV, release_mode=True)
    assert stats.get("quizzes_retired", 0) == 0
    assert stats["quizzes"] == 0
    assert db_session.get(Quiz, quiz.id).title == "Check 1"


# -- stats and transaction edges ------------------------------------------------------


def test_a_question_without_an_answer_key_is_counted_not_dropped(db_session):
    catalogue_course(db_session, "KEY-C1")
    doc = course_doc(
        "KEY-C1",
        [
            module_doc(
                1,
                quiz={
                    "title": "q",
                    "questions": [
                        {"question": "Open", "options": ["A) one", "B) two"], "answer": ""},
                    ],
                },
            )
        ],
    )
    stats = import_course_content(db_session, doc, DEV)
    assert stats["questions_without_key"] == 1
    question = db_session.query(QuizQuestion).one()
    assert json.loads(question.correct) == []


def test_a_stale_lab_reference_is_cleared_and_an_unreadable_one_left_alone(db_session):
    catalogue_course(db_session, "LAB-C1")
    ranged = course_doc("LAB-C1", [module_doc(1, activity="range", lab="Do the lab")])
    import_course_content(db_session, ranged, DEV)
    module = db_session.query(CourseModule).filter_by(course_id=the_course(db_session, "LAB-C1").id).one()
    assess = db_session.query(ModuleContent).filter_by(module_id=module.id, content_kind=ContentKind.assess).one()
    assert json.loads(assess.external_ref) == {"lab": "Do the lab"}
    # Re-imported as theory, a readable lab ref is cleared; an unreadable one is not touched.
    import_course_content(db_session, course_doc("LAB-C1", [module_doc(1)]), DEV)
    assert assess.external_ref == ""
    assess.external_ref = "{oops"
    import_course_content(db_session, course_doc("LAB-C1", [module_doc(1)]), DEV)
    assert assess.external_ref == "{oops"


def test_release_mode_keeps_publication_flags_and_a_plain_import_clears_them(db_session):
    course = catalogue_course(db_session, "PUB-C1")
    course.is_published = True
    db_session.flush()
    import_course_content(db_session, course_doc("PUB-C1", [module_doc(1)]), DEV)
    assert db_session.get(Course, course.id).is_published is False
    course.is_published = True
    db_session.flush()
    import_course_content(db_session, course_doc("PUB-C1", [module_doc(1)]), DEV, release_mode=True)
    assert db_session.get(Course, course.id).is_published is True


def test_commit_false_leaves_the_transaction_to_the_caller(db_session):
    catalogue_course(db_session, "ATOMIC-C1")
    stats = import_course_content(db_session, course_doc("ATOMIC-C1", [module_doc(1)]), DEV, commit=False)
    assert stats["modules_created"] == 1
    assert db_session.query(CourseModule).count() == 1
    db_session.rollback()
    assert db_session.query(CourseModule).count() == 0
