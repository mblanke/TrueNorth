"""Programme catalogue parsing + import guards.

The cyber-operator programme catalogue is an *unsourced draft*: its course codes,
titles and term dates were generated, not taken from an Algonquin/RMC programme
outline. These tests keep that fact structurally enforced rather than merely
documented — nothing from this file may reach a published or spine-bound state.

Mirrors the shape of test_qsp_crosswalk_integrity.py.
"""

import csv
import pathlib

import pytest
from app.models import Course, CourseModule
from app.programme_ingest import (
    EXPECTED_COLUMNS,
    course_name,
    import_programme,
    parse_programme,
)

CATALOGUE = pathlib.Path(__file__).resolve().parents[2] / "content/catalogue/cyber_operator_programme.csv"


def _csv_text() -> str:
    return CATALOGUE.read_text(encoding="utf-8")


# -- file integrity --------------------------------------------------------


def test_catalogue_exists_and_has_expected_columns():
    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == EXPECTED_COLUMNS


def test_every_row_declares_provenance_and_status():
    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows, "catalogue is empty"
    for r in rows:
        assert r["provenance"], f"{r['course_code']} has no provenance"
        assert r["status"], f"{r['course_code']} has no status"


def test_catalogue_is_entirely_unsourced():
    """If this fails, someone added sourced rows — update the draft banner in
    docs/cyber_operator_calendar.md and re-check what may now be published."""
    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        provenances = {r["provenance"] for r in csv.DictReader(f)}
    assert provenances == {"unsourced"}, f"unexpected provenance values: {provenances}"


def test_course_codes_are_unique():
    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        codes = [r["course_code"] for r in csv.DictReader(f)]
    dupes = {c for c in codes if codes.count(c) > 1}
    assert not dupes, f"duplicate course codes: {sorted(dupes)}"


# -- pure parser -----------------------------------------------------------


def test_parse_returns_a_row_per_course():
    rows = parse_programme(_csv_text())
    with open(CATALOGUE, newline="", encoding="utf-8") as f:
        assert len(rows) == len(list(csv.DictReader(f)))


def test_qsp_todo_sentinel_normalizes_to_none():
    """A placeholder must never be mistaken for a real qualification code."""
    for row in parse_programme(_csv_text()):
        assert row["qsp_code"] is None, (
            f"{row['course_code']} claims qsp_code={row['qsp_code']!r}; "
            "binding invented courses to a real QSP fabricates a CFITES claim"
        )


@pytest.mark.parametrize("sentinel", ["QSP-TODO", "qsp-todo", "", "-", "TODO", "n/a", "TBD"])
def test_sentinels_are_all_recognized(sentinel):
    text = (
        ",".join(EXPECTED_COLUMNS)
        + "\n"
        + ",".join(
            [
                "p",
                "inst",
                "1",
                sentinel,
                "T1",
                "Term 1",
                "2026-09-01",
                "2026-12-31",
                "15",
                "C101",
                "Test Course",
                "",
                "unsourced",
                "proposed",
            ]
        )
    )
    assert parse_programme(text)[0]["qsp_code"] is None


def test_real_qsp_code_is_preserved():
    text = (
        ",".join(EXPECTED_COLUMNS)
        + "\n"
        + ",".join(
            [
                "p",
                "inst",
                "1",
                "ALJQ",
                "T1",
                "Term 1",
                "2026-09-01",
                "2026-12-31",
                "15",
                "C101",
                "Test Course",
                "40",
                "sourced",
                "approved",
            ]
        )
    )
    assert parse_programme(text)[0]["qsp_code"] == "ALJQ"


def test_blank_duration_becomes_zero_not_a_guess():
    rows = parse_programme(_csv_text())
    assert all(r["duration_hours"] == 0 for r in rows)


def test_rows_without_a_course_code_are_skipped():
    text = (
        ",".join(EXPECTED_COLUMNS)
        + "\n"
        + ",".join(["p", "inst", "1", "-", "T1", "Term 1", "", "", "15", "", "No Code", "", "unsourced", "proposed"])
    )
    assert parse_programme(text) == []


def test_course_name_follows_the_qsp_paths_convention():
    assert course_name("C101", "Computer Architecture") == "C101 — Computer Architecture"
    assert course_name("", "Only Title") == "Only Title"


# -- import ----------------------------------------------------------------


def test_import_creates_a_course_per_row(db_session):
    stats = import_programme(db_session, _csv_text())
    assert stats["courses_created"] == stats["rows"]
    assert db_session.query(Course).count() == stats["rows"]


def test_imported_courses_are_never_published(db_session):
    """The guardrail. Unsourced content must not be publishable."""
    stats = import_programme(db_session, _csv_text())
    assert stats["published"] == 0
    assert db_session.query(Course).filter(Course.is_published.is_(True)).count() == 0


def test_imported_courses_are_unbound_from_the_spine(db_session):
    import_programme(db_session, _csv_text())
    bound = db_session.query(Course).filter(Course.qualification_id.isnot(None)).count()
    assert bound == 0


def test_import_creates_no_placeholder_modules(db_session):
    """Empty modules are honest; fabricated "Module 1..6" rows are not."""
    import_programme(db_session, _csv_text())
    assert db_session.query(CourseModule).count() == 0


def test_import_records_provenance_in_course_meta(db_session):
    import json

    import_programme(db_session, _csv_text())
    for course in db_session.query(Course).all():
        meta = json.loads(course.course_meta)
        assert meta["provenance"] == "unsourced"
        assert meta["course_code"]
        assert meta["term_code"]


def test_import_introduces_no_dp_tag_namespace(db_session):
    """dp_order on the Qualification carries DP1/DP2 — tags must not duplicate it."""
    import_programme(db_session, _csv_text())
    for course in db_session.query(Course).all():
        assert "dp1" not in course.tags.lower()
        assert "dp2" not in course.tags.lower()


def test_import_is_idempotent(db_session):
    first = import_programme(db_session, _csv_text())
    second = import_programme(db_session, _csv_text())
    assert second["courses_created"] == 0
    assert second["courses_updated"] == first["rows"]
    assert db_session.query(Course).count() == first["rows"]


# -- endpoint --------------------------------------------------------------


def test_import_endpoint_round_trips_the_real_catalogue(client):
    resp = client.post(
        "/courses/import-programme",
        files={"file": ("cyber_operator_programme.csv", _csv_text().encode(), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] is True
    assert body["courses_created"] == body["rows"]
    assert body["published"] == 0
    assert body["unbound_rows"] == body["rows"]


def test_import_endpoint_rejects_non_utf8(client):
    resp = client.post(
        "/courses/import-programme",
        files={"file": ("bad.csv", b"\xff\xfe\x00bad", "text/csv")},
    )
    assert resp.status_code == 422


def test_import_endpoint_rejects_oversize(client):
    from app.routers.courses import MAX_CSV_BYTES

    resp = client.post(
        "/courses/import-programme",
        files={"file": ("big.csv", b"a" * (MAX_CSV_BYTES + 1), "text/csv")},
    )
    assert resp.status_code == 413
