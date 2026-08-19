"""Guards on authored course content (content/courses/*.yaml).

The IoT Security Foundations course is a recovered fleet-authored draft. Its
pedagogy was kept; its citations were re-verified and its framework mappings were
dropped as invalid. These tests keep all three of those facts enforced.
"""

import json
import pathlib

import pytest
import yaml
from app.course_content_ingest import (
    answer_indices,
    import_course_content,
    parse_course_content,
)
from app.models import Course, CourseModule, Quiz, QuizQuestion
from app.programme_ingest import import_programme

ROOT = pathlib.Path(__file__).resolve().parents[2]
COURSE_DIR = ROOT / "content/courses"
COURSE_FILE = COURSE_DIR / "iot-security-foundations.yaml"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
REFERENCES = ROOT / "content/catalogue/references.yaml"

COURSE_FILES = sorted(COURSE_DIR.glob("*.yaml"))


def _text():
    return COURSE_FILE.read_text(encoding="utf-8")


def _doc():
    return yaml.safe_load(_text())


def _all_docs():
    return [(f.name, yaml.safe_load(f.read_text(encoding="utf-8"))) for f in COURSE_FILES]


def _library():
    return yaml.safe_load(REFERENCES.read_text(encoding="utf-8"))["references"]


# -- answer-key mapping ----------------------------------------------------


@pytest.mark.parametrize(
    "answer,expected",
    [("A", [0]), ("C", [2]), ("D", [3]), ("A,C", [0, 2]), ("", []), ("Z", []), ("?", [])],
)
def test_answer_letters_map_to_option_indices(answer, expected):
    assert answer_indices(answer, ["a", "b", "c", "d"]) == expected


def test_unrecognised_answer_yields_no_key_rather_than_a_guess():
    """A question with no key is reviewable; a wrong key mis-grades silently."""
    assert answer_indices("E", ["a", "b", "c", "d"]) == []


# -- file integrity --------------------------------------------------------


def test_every_course_file_is_a_draft_and_unpublished():
    """Unsourced content must never be publishable from the file itself."""
    for name, doc in _all_docs():
        assert doc["is_published"] is False, name
        assert doc["provenance"] == "unsourced", name
        assert doc["status"] in {"draft", "proposed"}, name


def test_no_course_file_is_bound_to_a_qualification():
    for name, doc in _all_docs():
        assert doc["qsp_code"] is None, f"{name} asserts a CFITES binding it cannot support"


def test_every_module_has_objectives_topics_and_a_lab():
    for name, doc in _all_docs():
        for m in doc["modules"]:
            assert m["objectives"], f"{name} module {m['ordinal']} has no objectives"
            assert m["topics"], f"{name} module {m['ordinal']} has no topics"
            assert m["lab"], f"{name} module {m['ordinal']} has no lab"


def test_every_reference_is_in_the_verified_library():
    """The recovered draft invented citations. Course files may only cite keys
    from content/catalogue/references.yaml, every entry of which was resolved
    against its publisher."""
    lib = _library()
    for name, doc in _all_docs():
        for m in doc["modules"]:
            assert m["refs"], f"{name} module {m['ordinal']} cites no references"
            for key in m["refs"]:
                assert key in lib, f"{name} cites unverified reference {key!r}"


def test_reference_library_entries_are_complete():
    for key, entry in _library().items():
        assert entry.get("url", "").startswith("https://"), f"{key} has no resolvable url"
        assert entry.get("title"), f"{key} has no title"
        assert entry.get("publisher"), f"{key} has no publisher"
        assert entry.get("year"), f"{key} has no year"


def test_withdrawn_publications_are_not_cited():
    """NIST SP 800-61 Rev. 2 was withdrawn 2025-04-03; Rev. 3 supersedes it.
    SP 800-63-3 is superseded by 800-63-4."""
    lib = _library()
    assert "nist-sp-800-61r2" not in lib
    assert "nist-sp-800-63-3" not in lib


def test_every_question_has_four_options_and_a_valid_key():
    for name, doc in _all_docs():
        for m in doc["modules"]:
            for q in m["quiz"]["questions"]:
                assert len(q["options"]) == 4, f"{name}: {q['question'][:50]}"
                assert q["answer"] in "ABCD", f"{name}: {q['question'][:50]}"


def test_no_duplicate_question_stems_across_the_library():
    """Duplicated questions are the signature of copy-paste filler."""
    seen: dict[str, str] = {}
    for name, doc in _all_docs():
        for m in doc["modules"]:
            for q in m["quiz"]["questions"]:
                stem = q["question"]
                assert stem not in seen, f"duplicate question in {name} and {seen[stem]}: {stem[:60]}"
                seen[stem] = name


def test_every_catalogue_course_has_content():
    import csv

    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        catalogue = {r["course_code"] for r in csv.DictReader(f)}
    authored = {doc["course_code"] for _, doc in _all_docs()}
    assert catalogue == authored, f"catalogue/content mismatch: {catalogue ^ authored}"


def test_no_fabricated_framework_mappings_are_present():
    """The source draft asserted 'DE-RS-01' and identical NICE/CSF mappings for
    every course. Framework mapping belongs to crosswalk.csv, not to content."""
    # Check the content itself, not the source.notes changelog, which quotes the
    # bad identifier precisely to record that it was removed.
    for name, doc in _all_docs():
        blob = yaml.safe_dump(doc["modules"], allow_unicode=True).lower()
        for bad in ("de-rs-", "dcwf", "cc-3", "nice_dcwf", "csf sub-categor"):
            assert bad not in blob, f"{name}: fabricated framework mapping leaked back in: {bad}"


def test_the_miscited_publications_are_gone():
    """NISTIR 8286 is ERM, not IoT; SP 800-183 is 'Networks of Things', not a
    2024 threat landscape; OWASP IoT items are I1-I10, not T1/T9."""
    for f in COURSE_FILES:
        blob = f.read_text(encoding="utf-8")
        assert "8286" not in blob, f.name
        assert "IoT Threat Landscape" not in blob, f.name
        assert "T9 (Insecure Network Services)" not in blob, f.name


# -- parser ----------------------------------------------------------------


def test_parse_strips_option_letter_prefixes():
    parsed = parse_course_content(_text())
    first = parsed["modules"][0]["questions"][0]
    assert first["options"] == ["MQTT", "CoAP", "SNMP", "LwM2M"]
    assert first["correct"] == [2]  # SNMP


def test_parse_requires_a_course_code():
    with pytest.raises(ValueError, match="course_code"):
        parse_course_content("title: nope\n")


def test_every_question_has_an_answer_key():
    parsed = parse_course_content(_text())
    for m in parsed["modules"]:
        for q in m["questions"]:
            assert q["correct"], f"question without a key: {q['stem'][:60]}"


# -- import ----------------------------------------------------------------


@pytest.fixture
def seeded(db_session):
    import_programme(db_session, CATALOGUE.read_text(encoding="utf-8"))
    return db_session


def test_import_attaches_modules_and_quizzes(seeded):
    stats = import_course_content(seeded, _text())
    assert stats["modules_created"] == 6
    assert stats["quizzes"] == 6
    assert stats["questions"] == 30
    assert stats["questions_without_key"] == 0


def test_import_leaves_everything_unpublished(seeded):
    import_course_content(seeded, _text())
    course = seeded.query(Course).filter(Course.name.like("C304%")).one()
    assert course.is_published is False
    assert seeded.query(Quiz).filter(Quiz.is_published.is_(True)).count() == 0


def test_import_writes_no_competency_codes(seeded):
    """Framework mapping stays with the auditable crosswalk, not generated content."""
    import_course_content(seeded, _text())
    for q in seeded.query(QuizQuestion).all():
        assert q.competency_code == ""


def test_import_is_idempotent(seeded):
    import_course_content(seeded, _text())
    second = import_course_content(seeded, _text())
    assert second["modules_created"] == 0
    assert second["modules_updated"] == 6
    assert seeded.query(CourseModule).count() == 6
    assert seeded.query(QuizQuestion).count() == 30


def test_import_preserves_provenance_on_the_course(seeded):
    import_course_content(seeded, _text())
    course = seeded.query(Course).filter(Course.name.like("C304%")).one()
    meta = json.loads(course.course_meta)
    assert meta["content_provenance"] == "unsourced"
    assert meta["provenance"] == "unsourced"


def test_import_refuses_when_the_catalogue_course_is_absent(db_session):
    """Content must not invent a course; the catalogue is the source of courses."""
    with pytest.raises(ValueError, match="import the programme catalogue"):
        import_course_content(db_session, _text())


def test_import_endpoint(client):
    client.post(
        "/courses/import-programme",
        files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")},
    )
    resp = client.post(
        "/courses/import-course-content",
        files={"file": ("iot.yaml", _text().encode(), "application/x-yaml")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == 30
